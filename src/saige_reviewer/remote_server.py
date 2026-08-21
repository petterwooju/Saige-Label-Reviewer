from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .adapters import load_visionproj, render_preview
from .analysis import AnalysisConfig, analyze
from .domain import Dataset, ReviewItem
from .server import ThreadingHTTPServer, _acquire_instance_lock, _release_instance_lock


REMOTE_STATIC = Path(__file__).parent / "remote_static"
REMOTE_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
DEFAULT_MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024
DEFAULT_RETENTION_SECONDS = 24 * 60 * 60
DEFAULT_MAX_STORAGE_SIZE = 20 * 1024 * 1024 * 1024
DEFAULT_MIN_FREE_SPACE = 5 * 1024 * 1024 * 1024
MAX_JSON_BODY = 64 * 1024
MAX_RESULT_PAGE = 200
ACTIVE_STATES = frozenset({"uploading", "validating", "queued", "running"})
FINAL_STATES = frozenset({"completed", "failed"})
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _now() -> float:
    return time.time()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_filename(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("filename must be a string")
    name = value.strip()
    if (
        not name
        or len(name) > 240
        or name != Path(name).name
        or any(ord(character) < 32 for character in name)
        or Path(name).suffix.lower() != ".visionproj"
    ):
        raise ValueError("only a plain .visionproj filename is allowed")
    return name


def _normalize_email(value: object, allowed_domain: str) -> str:
    if not isinstance(value, str):
        raise PermissionError("Cloudflare Access identity is missing")
    email = value.strip().lower()
    local, separator, domain = email.rpartition("@")
    if not separator or not local or domain != allowed_domain.lower():
        raise PermissionError(f"only @{allowed_domain} accounts are allowed")
    if len(email) > 254 or any(character.isspace() for character in email):
        raise PermissionError("invalid email identity")
    return email


@dataclass(slots=True)
class RemoteJob:
    id: str
    owner: str
    filename: str
    total_size: int
    chunk_count: int
    state: str = "uploading"
    received_chunks: list[int] = field(default_factory=list)
    progress: float = 0.0
    message: str = "Waiting for upload"
    error: str | None = None
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    completed_at: float | None = None
    input_sha256: str | None = None
    summary: dict | None = None

    @classmethod
    def from_dict(cls, value: dict) -> "RemoteJob":
        fields = cls.__dataclass_fields__
        return cls(**{key: value[key] for key in fields if key in value})

    def public(self, retention_seconds: int) -> dict:
        return {
            "id": self.id,
            "filename": self.filename,
            "total_size": self.total_size,
            "chunk_count": self.chunk_count,
            "received_chunks": len(self.received_chunks),
            "state": self.state,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "expires_at": (
                self.completed_at + retention_seconds
                if self.completed_at is not None else None
            ),
            "input_sha256": self.input_sha256,
            "summary": self.summary,
            "result_ready": self.state == "completed" and self.summary is not None,
        }


class RemoteJobManager:
    """Isolated upload storage plus a single analysis worker."""

    def __init__(
        self,
        root: Path,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_upload_size: int = DEFAULT_MAX_UPLOAD_SIZE,
        max_storage_size: int = DEFAULT_MAX_STORAGE_SIZE,
        min_free_space: int = DEFAULT_MIN_FREE_SPACE,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
        analyzer=analyze,
        loader=load_visionproj,
        start_worker: bool = True,
    ):
        self.root = root.resolve()
        self.jobs_root = self.root / "jobs"
        self.cache_root = self.root / "cache"
        self.chunk_size = int(chunk_size)
        self.max_upload_size = int(max_upload_size)
        self.max_storage_size = int(max_storage_size)
        self.min_free_space = int(min_free_space)
        self.retention_seconds = int(retention_seconds)
        if (
            self.chunk_size <= 0
            or self.max_upload_size <= 0
            or self.max_storage_size < self.max_upload_size
            or self.min_free_space < 0
            or self.retention_seconds <= 0
        ):
            raise ValueError("remote limits must be positive")
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._analyzer = analyzer
        self._loader = loader
        self._lock = threading.RLock()
        self._jobs: dict[str, RemoteJob] = {}
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._closed = threading.Event()
        self._worker: threading.Thread | None = None
        self._cleaner: threading.Thread | None = None
        self._load_existing()
        self.cleanup_expired()
        if start_worker:
            self._worker = threading.Thread(
                target=self._worker_loop, name="saige-remote-analysis", daemon=True
            )
            self._worker.start()
            self._cleaner = threading.Thread(
                target=self._cleanup_loop, name="saige-remote-cleanup", daemon=True
            )
            self._cleaner.start()

    def _job_dir(self, job_id: str) -> Path:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise ValueError("invalid job id")
        target = (self.jobs_root / job_id).resolve()
        if target.parent != self.jobs_root:
            raise ValueError("invalid job path")
        return target

    def _job_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "job.json"

    def _save(self, job: RemoteJob) -> None:
        _atomic_json(self._job_path(job.id), asdict(job))

    def _load_existing(self) -> None:
        for directory in self.jobs_root.iterdir():
            if not directory.is_dir() or not JOB_ID_PATTERN.fullmatch(directory.name):
                continue
            try:
                value = json.loads((directory / "job.json").read_text(encoding="utf-8"))
                job = RemoteJob.from_dict(value)
                if job.id != directory.name or job.state not in ACTIVE_STATES | FINAL_STATES:
                    continue
                job.received_chunks = sorted({int(index) for index in job.received_chunks})
                if job.state in {"validating", "queued", "running"}:
                    job.state = "failed"
                    job.error = "The server restarted before analysis completed. Upload again."
                    job.message = "Interrupted by server restart"
                    job.completed_at = _now()
                    job.updated_at = job.completed_at
                    self._save(job)
                self._jobs[job.id] = job
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._worker is not None:
            self._queue.put(None)
            self._worker.join(timeout=10)
        if self._cleaner is not None:
            self._cleaner.join(timeout=10)

    def _cleanup_loop(self) -> None:
        interval = max(30, min(300, self.retention_seconds // 4))
        while not self._closed.wait(interval):
            self.cleanup_expired()

    def cleanup_expired(self, now: float | None = None) -> list[str]:
        current = _now() if now is None else float(now)
        removed: list[str] = []
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.state in {"validating", "queued", "running"}:
                    continue
                expiry_base = job.completed_at or job.updated_at
                if current - expiry_base < self.retention_seconds:
                    continue
                target = self._job_dir(job_id)
                del self._jobs[job_id]
                try:
                    shutil.rmtree(target)
                except OSError:
                    self._jobs[job_id] = job
                    continue
                removed.append(job_id)
        return removed

    def _public(self, job: RemoteJob) -> dict:
        return job.public(self.retention_seconds)

    def _safe_error(self, error: Exception) -> str:
        if isinstance(error, (ValueError, RuntimeError)):
            message = str(error).strip() or type(error).__name__
        else:
            message = f"{type(error).__name__} while processing the project"
        for candidate in {self.root, Path.cwd().resolve(), Path.home().resolve()}:
            for text in {str(candidate), candidate.as_posix()}:
                message = re.sub(re.escape(text), "<local-path>", message, flags=re.IGNORECASE)
        return message[:2000]

    def _owned(self, owner: str, job_id: str) -> RemoteJob:
        job = self._jobs.get(job_id)
        if job is None or not secrets.compare_digest(job.owner, owner):
            raise LookupError("job not found")
        return job

    def create_upload(self, owner: str, filename: object, total_size: object, chunk_count: object) -> dict:
        self.cleanup_expired()
        name = _safe_filename(filename)
        if isinstance(total_size, bool) or isinstance(chunk_count, bool):
            raise ValueError("upload size and chunk count must be integers")
        try:
            size = int(total_size)
            count = int(chunk_count)
        except (TypeError, ValueError) as error:
            raise ValueError("upload size and chunk count must be integers") from error
        if size <= 0 or size > self.max_upload_size:
            raise ValueError(f"upload must be between 1 byte and {self.max_upload_size} bytes")
        expected_count = (size + self.chunk_size - 1) // self.chunk_size
        if count != expected_count:
            raise ValueError("chunk count does not match upload size")
        with self._lock:
            if any(job.owner == owner and job.state in ACTIVE_STATES for job in self._jobs.values()):
                raise RuntimeError("this account already has an active upload or analysis job")
            reserved = sum(job.total_size for job in self._jobs.values())
            if reserved + size > self.max_storage_size:
                raise RuntimeError("remote storage quota is currently full; try again later")
            if shutil.disk_usage(self.root).free < size + self.min_free_space:
                raise RuntimeError("the server does not have enough free disk space for this upload")
            for _attempt in range(8):
                job_id = uuid.uuid4().hex
                directory = self._job_dir(job_id)
                try:
                    directory.mkdir(exist_ok=False)
                    (directory / "chunks").mkdir(exist_ok=False)
                    break
                except FileExistsError:
                    continue
            else:
                raise OSError("could not reserve a unique job directory")
            job = RemoteJob(job_id, owner, name, size, count)
            self._jobs[job_id] = job
            try:
                self._save(job)
            except Exception:
                del self._jobs[job_id]
                shutil.rmtree(directory, ignore_errors=True)
                raise
            return {**self._public(job), "chunk_size": self.chunk_size}

    def write_chunk(
        self, owner: str, job_id: str, index: int, payload: bytes, expected_sha256: str
    ) -> dict:
        if not isinstance(payload, bytes):
            raise ValueError("chunk body must be bytes")
        expected_sha256 = str(expected_sha256 or "").lower()
        if not SHA256_PATTERN.fullmatch(expected_sha256):
            raise ValueError("X-Chunk-SHA256 must be a lowercase SHA-256 digest")
        with self._lock:
            job = self._owned(owner, job_id)
            if job.state != "uploading":
                raise RuntimeError("upload is no longer accepting chunks")
            if index < 0 or index >= job.chunk_count:
                raise ValueError("chunk index is out of range")
            expected_size = (
                self.chunk_size
                if index < job.chunk_count - 1
                else job.total_size - self.chunk_size * (job.chunk_count - 1)
            )
            if len(payload) != expected_size:
                raise ValueError("chunk size does not match its declared position")
            actual_sha256 = hashlib.sha256(payload).hexdigest()
            if not secrets.compare_digest(actual_sha256, expected_sha256):
                raise ValueError("chunk SHA-256 mismatch")
            target = self._job_dir(job_id) / "chunks" / f"{index:08d}.part"
            if index in job.received_chunks or target.exists():
                raise RuntimeError("chunk was already uploaded")
            try:
                with target.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                target.unlink(missing_ok=True)
                raise
            job.received_chunks.append(index)
            job.received_chunks.sort()
            job.progress = len(job.received_chunks) / job.chunk_count
            job.message = f"Uploaded {len(job.received_chunks)} of {job.chunk_count} chunks"
            job.updated_at = _now()
            self._save(job)
            return self._public(job)

    def complete_upload(self, owner: str, job_id: str, expected_sha256: object = None) -> dict:
        expected = str(expected_sha256 or "").lower()
        if expected and not SHA256_PATTERN.fullmatch(expected):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        with self._lock:
            job = self._owned(owner, job_id)
            if job.state != "uploading":
                raise RuntimeError("upload cannot be completed in its current state")
            if job.received_chunks != list(range(job.chunk_count)):
                raise ValueError("upload is incomplete")
            job.state = "validating"
            job.progress = 1.0
            job.message = "Validating the assembled visionproj"
            job.updated_at = _now()
            self._save(job)
            directory = self._job_dir(job_id)
            temporary = directory / f".input.tmp-{uuid.uuid4().hex}"
            final = directory / "input.visionproj"
            digest = hashlib.sha256()
            written = 0
        try:
            with temporary.open("xb") as output:
                for index in range(job.chunk_count):
                    part = directory / "chunks" / f"{index:08d}.part"
                    with part.open("rb") as source:
                        while True:
                            block = source.read(1024 * 1024)
                            if not block:
                                break
                            written += len(block)
                            if written > job.total_size:
                                raise ValueError("assembled upload exceeds declared size")
                            digest.update(block)
                            output.write(block)
                output.flush()
                os.fsync(output.fileno())
            actual = digest.hexdigest()
            if written != job.total_size:
                raise ValueError("assembled upload size mismatch")
            if expected and not secrets.compare_digest(actual, expected):
                raise ValueError("full upload SHA-256 mismatch")
            os.replace(temporary, final)
            self._loader(final)
        except Exception as error:
            temporary.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            safe_error = self._safe_error(error)
            print(f"remote upload validation failed: {type(error).__name__}: {error}")
            with self._lock:
                job = self._owned(owner, job_id)
                job.state = "failed"
                job.error = safe_error
                job.message = "Upload validation failed"
                job.completed_at = _now()
                job.updated_at = job.completed_at
                self._save(job)
            raise ValueError(safe_error) from None
        with self._lock:
            job = self._owned(owner, job_id)
            if job.state != "validating":
                raise RuntimeError("upload validation state changed unexpectedly")
            shutil.rmtree(directory / "chunks", ignore_errors=True)
            job.input_sha256 = actual
            job.state = "queued"
            job.progress = 0.0
            job.message = "Waiting for the local GPU analysis queue"
            job.updated_at = _now()
            self._save(job)
            self._queue.put(job.id)
            return self._public(job)

    def list_jobs(self, owner: str) -> list[dict]:
        self.cleanup_expired()
        with self._lock:
            jobs = [self._public(job) for job in self._jobs.values() if job.owner == owner]
        return sorted(jobs, key=lambda value: value["created_at"], reverse=True)

    def get_job(self, owner: str, job_id: str) -> dict:
        with self._lock:
            return self._public(self._owned(owner, job_id))

    def delete_job(self, owner: str, job_id: str) -> None:
        with self._lock:
            job = self._owned(owner, job_id)
            if job.state in {"validating", "running"}:
                raise RuntimeError("a validating or running job cannot be deleted")
            target = self._job_dir(job_id)
            del self._jobs[job_id]
            try:
                shutil.rmtree(target)
            except OSError:
                self._jobs[job_id] = job
                raise

    def _set_progress(self, job_id: str, value: float, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state != "running":
                return
            job.progress = max(0.0, min(float(value), 1.0))
            job.message = str(message)[:500]
            job.updated_at = _now()
            self._save(job)

    def _worker_loop(self) -> None:
        while not self._closed.is_set():
            job_id = self._queue.get()
            try:
                if job_id is None:
                    return
                self._run_job(job_id)
            finally:
                self._queue.task_done()

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state != "queued":
                return
            job.state = "running"
            job.progress = 0.0
            job.message = "Loading the uploaded visionproj"
            job.updated_at = _now()
            self._save(job)
        try:
            source = self._job_dir(job_id) / "input.visionproj"
            dataset = self._loader(source)
            config = AnalysisConfig(device="auto", model_access="local_only")
            result = self._analyzer(
                dataset,
                config,
                self._job_dir(job_id) / "analysis-cache",
                lambda value, message: self._set_progress(job_id, value, message),
            )
            summary = self._store_results(job_id, dataset, result)
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                job.state = "completed"
                job.progress = 1.0
                job.message = "Analysis completed"
                job.error = None
                job.summary = summary
                job.completed_at = _now()
                job.updated_at = job.completed_at
                self._save(job)
        except Exception as error:
            safe_error = self._safe_error(error)
            print(f"remote analysis failed for {job_id}: {type(error).__name__}: {error}")
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                job.state = "failed"
                job.error = safe_error
                job.message = "Analysis failed"
                job.completed_at = _now()
                job.updated_at = job.completed_at
                self._save(job)

    def _store_results(self, job_id: str, dataset: Dataset, result: dict) -> dict:
        updates = result.get("item_updates")
        if not isinstance(updates, list) or len(updates) != len(dataset.items):
            raise ValueError("analysis returned an incomplete item update set")
        by_id = {str(update.get("id")): update for update in updates if isinstance(update, dict)}
        if len(by_id) != len(dataset.items):
            raise ValueError("analysis returned duplicate or invalid item ids")
        target = self._job_dir(job_id) / "results.sqlite3"
        temporary = target.with_name(f".{target.name}.tmp-{uuid.uuid4().hex}")
        try:
            connection = sqlite3.connect(temporary)
            try:
                connection.execute(
                    "CREATE TABLE items ("
                    "id TEXT PRIMARY KEY, relative_path TEXT NOT NULL, original_label TEXT NOT NULL, "
                    "suggested_label TEXT, suspicion_score REAL NOT NULL, x REAL NOT NULL, y REAL NOT NULL, "
                    "label_confidence REAL, neighbor_support REAL, metadata_json TEXT NOT NULL)"
                )
                rows = []
                for item in dataset.items:
                    update = by_id.get(item.id)
                    if update is None:
                        raise ValueError(f"analysis result is missing item {item.id}")
                    rows.append(
                        (
                            item.id,
                            item.relative_path,
                            item.original_label,
                            update.get("suggested_label"),
                            float(update.get("suspicion_score")),
                            float(update.get("x")),
                            float(update.get("y")),
                            float(update.get("label_confidence")),
                            float(update.get("neighbor_support")),
                            json.dumps(
                                item.metadata,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                default=str,
                            ),
                        )
                    )
                connection.executemany("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
                connection.execute(
                    "CREATE INDEX score_order ON items(suspicion_score DESC, id ASC)"
                )
                connection.commit()
            finally:
                connection.close()
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        execution = result.get("execution") or {}
        return {
            "dataset_name": dataset.name,
            "item_count": len(dataset.items),
            "classes": list(dataset.classes),
            "effective_input_size": result.get("effective_input_size"),
            "effective_projection": result.get("effective_projection"),
            "cache_hit": bool(result.get("cache_hit")),
            "algorithm_version": result.get("algorithm_version"),
            "device": execution.get("device"),
            "dtype": execution.get("dtype"),
        }

    def query_results(self, owner: str, job_id: str, page: int, limit: int) -> dict:
        if page < 1 or limit < 1 or limit > MAX_RESULT_PAGE:
            raise ValueError(f"page must be positive and limit must be 1..{MAX_RESULT_PAGE}")
        with self._lock:
            job = self._owned(owner, job_id)
            if job.state != "completed" or not job.summary:
                raise RuntimeError("results are not ready")
            summary = dict(job.summary)
            public_job = self._public(job)
            database = self._job_dir(job_id) / "results.sqlite3"
            connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                offset = (page - 1) * limit
                rows = connection.execute(
                    "SELECT id, relative_path, original_label, suggested_label, suspicion_score, "
                    "x, y, label_confidence, neighbor_support FROM items "
                    "ORDER BY suspicion_score DESC, id ASC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            finally:
                connection.close()
        items = []
        for row in rows:
            item = dict(row)
            item["preview_url"] = f"/api/jobs/{job_id}/preview/{item['id']}"
            items.append(item)
        return {
            "job": public_job,
            "summary": summary,
            "page": page,
            "limit": limit,
            "items": items,
        }

    def preview(
        self, owner: str, job_id: str, item_id: str, mode: str, show_contours: bool
    ):
        with self._lock:
            job = self._owned(owner, job_id)
            if job.state != "completed" or not job.summary:
                raise RuntimeError("results are not ready")
            summary = dict(job.summary)
            database = self._job_dir(job_id) / "results.sqlite3"
            connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                row = connection.execute(
                    "SELECT id, relative_path, original_label, metadata_json FROM items WHERE id=?",
                    (item_id,),
                ).fetchone()
            finally:
                connection.close()
            if row is None:
                raise LookupError("item not found")
            source = self._job_dir(job_id) / "input.visionproj"
            item = ReviewItem(
                str(row["id"]), str(row["relative_path"]), str(row["original_label"]),
                None, None, 0.0, 0.0, metadata=json.loads(row["metadata_json"]),
            )
            dataset = Dataset(
                str(summary.get("dataset_name") or job.filename), source, job.input_sha256,
                list(summary.get("classes") or []), [item], "visionproj:remote",
            )
            return render_preview(dataset, item, mode, show_contours=show_contours)


@dataclass(frozen=True, slots=True)
class RemoteServerConfig:
    expected_hostname: str
    allowed_domain: str = "saigeai.com"
    dev_auth_email: str | None = None

    def __post_init__(self):
        hostname = self.expected_hostname.strip().lower().rstrip(".")
        domain = self.allowed_domain.strip().lower().lstrip("@").rstrip(".")
        if not hostname or not domain:
            raise ValueError("expected hostname and allowed domain are required")
        object.__setattr__(self, "expected_hostname", hostname)
        object.__setattr__(self, "allowed_domain", domain)
        if self.dev_auth_email is not None:
            object.__setattr__(
                self, "dev_auth_email", _normalize_email(self.dev_auth_email, domain)
            )


def create_remote_handler(manager: RemoteJobManager, config: RemoteServerConfig):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"SaigeRemote/{__version__}"
        sys_version = ""

        def log_message(self, fmt, *args):
            print(f"[remote {self.log_date_time_string()}] {fmt % args}")

        def _send(self, body: bytes, content_type: str, status=HTTPStatus.OK):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'; object-src 'none'",
            )
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, value, status=HTTPStatus.OK):
            self._send(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                "application/json; charset=utf-8",
                status,
            )

        def _host(self) -> str:
            return (self.headers.get("Host") or "").split(":", 1)[0].lower().rstrip(".")

        def _identity(self) -> str:
            host = self._host()
            if config.dev_auth_email and host in {"127.0.0.1", "localhost"}:
                return config.dev_auth_email
            if host != config.expected_hostname:
                raise PermissionError("unexpected public hostname")
            assertion = self.headers.get("Cf-Access-Jwt-Assertion") or ""
            if len(assertion) < 20 or len(assertion) > 16_384:
                raise PermissionError("Cloudflare Access assertion is missing")
            return _normalize_email(
                self.headers.get("Cf-Access-Authenticated-User-Email"),
                config.allowed_domain,
            )

        def _read_json(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or "-1")
            except ValueError as error:
                raise ValueError("invalid Content-Length") from error
            if length < 0 or length > MAX_JSON_BODY:
                raise ValueError("JSON request body is too large")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload

        def _route_error(self, error: Exception):
            if isinstance(error, PermissionError):
                return self._json({"error": str(error)}, HTTPStatus.FORBIDDEN)
            if isinstance(error, LookupError):
                return self._json({"error": str(error)}, HTTPStatus.NOT_FOUND)
            if isinstance(error, RuntimeError):
                return self._json({"error": str(error)}, HTTPStatus.CONFLICT)
            if isinstance(error, (ValueError, OSError, json.JSONDecodeError)):
                return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            print(f"remote request failed: {type(error).__name__}: {error}")
            return self._json({"error": "internal server error"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            try:
                owner = self._identity()
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/api/me":
                    return self._json(
                        {
                            "email": owner,
                            "allowed_domain": config.allowed_domain,
                            "chunk_size": manager.chunk_size,
                            "max_upload_size": manager.max_upload_size,
                            "max_storage_size": manager.max_storage_size,
                            "retention_seconds": manager.retention_seconds,
                        }
                    )
                if path == "/api/jobs":
                    return self._json({"jobs": manager.list_jobs(owner)})
                match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})", path)
                if match:
                    return self._json({"job": manager.get_job(owner, match.group(1))})
                match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/results", path)
                if match:
                    query = parse_qs(parsed.query)
                    page = int((query.get("page") or ["1"])[0])
                    limit = int((query.get("limit") or ["100"])[0])
                    return self._json(manager.query_results(owner, match.group(1), page, limit))
                match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/preview/([^/]+)", path)
                if match:
                    query = parse_qs(parsed.query)
                    mode = (query.get("view") or ["crop"])[0]
                    if mode not in {"crop", "original"}:
                        raise ValueError("invalid preview mode")
                    show_contours = (query.get("contours") or ["1"])[0] != "0"
                    preview = manager.preview(
                        owner, match.group(1), unquote(match.group(2)), mode, show_contours
                    )
                    if preview is None:
                        return self._send(b"", "image/png", HTTPStatus.NO_CONTENT)
                    return self._send(*preview)
                name = "remote.html" if path == "/" else path.lstrip("/")
                if name not in {"remote.html", "remote.css", "remote.js"}:
                    return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                target = (REMOTE_STATIC / name).resolve()
                if target.parent != REMOTE_STATIC.resolve() or not target.is_file():
                    return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                content_type = REMOTE_TYPES.get(
                    target.suffix.lower(),
                    mimetypes.guess_type(target.name)[0] or "application/octet-stream",
                )
                return self._send(target.read_bytes(), content_type)
            except Exception as error:
                return self._route_error(error)

        def do_POST(self):
            try:
                owner = self._identity()
                path = urlparse(self.path).path
                body = self._read_json()
                if path == "/api/uploads":
                    return self._json(
                        manager.create_upload(
                            owner,
                            body.get("filename"),
                            body.get("total_size"),
                            body.get("chunk_count"),
                        ),
                        HTTPStatus.CREATED,
                    )
                match = re.fullmatch(r"/api/uploads/([0-9a-f]{32})/complete", path)
                if match:
                    return self._json(
                        {"job": manager.complete_upload(owner, match.group(1), body.get("sha256"))}
                    )
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except Exception as error:
                return self._route_error(error)

        def do_PUT(self):
            try:
                owner = self._identity()
                path = urlparse(self.path).path
                match = re.fullmatch(r"/api/uploads/([0-9a-f]{32})/chunks/(\d+)", path)
                if not match:
                    return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                try:
                    length = int(self.headers.get("Content-Length") or "-1")
                except ValueError as error:
                    raise ValueError("invalid Content-Length") from error
                if length < 0 or length > manager.chunk_size:
                    raise ValueError("chunk body is too large")
                payload = self.rfile.read(length)
                if len(payload) != length:
                    raise ValueError("incomplete chunk body")
                job = manager.write_chunk(
                    owner,
                    match.group(1),
                    int(match.group(2)),
                    payload,
                    self.headers.get("X-Chunk-SHA256") or "",
                )
                return self._json({"job": job})
            except Exception as error:
                return self._route_error(error)

        def do_DELETE(self):
            try:
                owner = self._identity()
                path = urlparse(self.path).path
                match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})", path)
                if not match:
                    return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                manager.delete_job(owner, match.group(1))
                return self._json({"deleted": True})
            except Exception as error:
                return self._route_error(error)

    return Handler


def run_remote(
    *,
    port: int,
    expected_hostname: str,
    allowed_domain: str,
    workspace: Path,
    dev_auth_email: str | None = None,
) -> None:
    workspace = workspace.resolve()
    instance_lock = _acquire_instance_lock(workspace)
    manager = RemoteJobManager(workspace)
    server = None
    try:
        config = RemoteServerConfig(expected_hostname, allowed_domain, dev_auth_email)
        server = ThreadingHTTPServer(
            ("127.0.0.1", int(port)), create_remote_handler(manager, config)
        )
        actual_port = int(server.server_address[1])
        print(f"Saige remote analysis origin: http://127.0.0.1:{actual_port}")
        print(f"Expected public hostname: {config.expected_hostname}")
        print(f"Allowed email domain: @{config.allowed_domain}")
        print("The origin is loopback-only; publish it only through Cloudflare Access + Tunnel.")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.server_close()
        manager.close()
        _release_instance_lock(instance_lock)


def main() -> None:
    parser = argparse.ArgumentParser(description="Saige Label Reviewer remote upload service")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument(
        "--expected-hostname", default="saige-label-reviewer-beta.saigeai.com"
    )
    parser.add_argument("--allowed-domain", default="saigeai.com")
    parser.add_argument("--workspace", type=Path, default=Path.cwd() / "workspace" / "remote")
    parser.add_argument(
        "--dev-auth-email",
        help="Loopback-only development identity; never use this on the tunnel service",
    )
    args = parser.parse_args()
    run_remote(
        port=args.port,
        expected_hostname=args.expected_hostname,
        allowed_domain=args.allowed_domain,
        workspace=args.workspace,
        dev_auth_email=args.dev_auth_email,
    )


if __name__ == "__main__":
    main()
