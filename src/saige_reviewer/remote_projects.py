from __future__ import annotations

import gc
import hashlib
import hmac
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import sqlite3
import stat
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from . import __version__
from .adapters import (
    IMAGE_EXTENSIONS,
    MAX_ARCHIVE_ENTRIES,
    MAX_DATASET_ITEMS,
    load_source,
    render_preview,
)
from .analysis import AnalysisConfig, analyze, dependency_status, model_status
from .exporter import export_corrected
from .session import ReviewSession


API_VERSION = 2
CHUNK_SIZE = 8 * 1024 * 1024
MAX_PROJECT_SIZE = 20 * 1024 * 1024 * 1024
MAX_MANAGED_STORAGE = 1024 * 1024 * 1024 * 1024
MIN_FREE_SPACE = 200 * 1024 * 1024 * 1024
RETENTION_SECONDS = 7 * 24 * 60 * 60
RECYCLE_SECONDS = 24 * 60 * 60
LEASE_SECONDS = 120
VIEW_TOUCH_INTERVAL = 60 * 60
MAX_UPLOAD_BATCH = 500
MAX_JSON_BODY = 8 * 1024 * 1024
ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FORMATS = frozenset({"visionproj", "srproj", "saige-json", "folder"})


def _now() -> float:
    return time.time()


def _json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_manifest(root: Path) -> dict[str, dict[str, object]]:
    manifest: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("远程项目源目录不能包含符号链接或联接")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        manifest[relative] = {"size": path.stat().st_size, "sha256": _stream_sha256(path)}
    return manifest


def _safe_id(value: object, description: str) -> str:
    text = str(value or "")
    if not ID_RE.fullmatch(text):
        raise ValueError(f"invalid {description}")
    return text


def _safe_logical_path(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("logical_path must be a string")
    normalized = value.replace("\\", "/").strip("/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or len(normalized) > 1024
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(ord(character) < 32 for character in normalized)
        or ":" in path.parts[0]
    ):
        raise ValueError(f"unsafe logical path: {value}")
    return path.as_posix()


def _safe_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("project name must be a string")
    name = value.strip()
    if not name or len(name) > 160 or any(ord(character) < 32 for character in name):
        raise ValueError("project name is invalid")
    return name


def _as_int(value: object, name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if result < minimum or (maximum is not None and result > maximum):
        raise ValueError(f"{name} is out of range")
    return result


@dataclass(frozen=True, slots=True)
class RemoteRuntimeConfig:
    root: Path
    admin_emails: frozenset[str]
    max_project_size: int = MAX_PROJECT_SIZE
    max_managed_storage: int = MAX_MANAGED_STORAGE
    min_free_space: int = MIN_FREE_SPACE
    retention_seconds: int = RETENTION_SECONDS
    recycle_seconds: int = RECYCLE_SECONDS
    lease_seconds: int = LEASE_SECONDS

    @classmethod
    def load(cls, root: Path) -> "RemoteRuntimeConfig":
        root = root.resolve()
        path = root / "config" / "remote-config.json"
        if not path.is_file():
            return cls(root=root, admin_emails=frozenset())
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("remote-config.json must contain an object")
        admins = value.get("admin_emails") or []
        if not isinstance(admins, list):
            raise ValueError("admin_emails must be an array")
        normalized = frozenset(str(email).strip().lower() for email in admins if str(email).strip())
        return cls(
            root=root,
            admin_emails=normalized,
            max_project_size=_as_int(
                value.get("max_project_size", MAX_PROJECT_SIZE),
                "max_project_size",
                minimum=1,
                maximum=MAX_PROJECT_SIZE,
            ),
            max_managed_storage=_as_int(
                value.get("max_managed_storage", MAX_MANAGED_STORAGE),
                "max_managed_storage",
                minimum=MAX_PROJECT_SIZE,
            ),
            min_free_space=_as_int(
                value.get("min_free_space", MIN_FREE_SPACE), "min_free_space", minimum=0
            ),
            retention_seconds=_as_int(
                value.get("retention_seconds", RETENTION_SECONDS),
                "retention_seconds",
                minimum=60,
            ),
            recycle_seconds=_as_int(
                value.get("recycle_seconds", RECYCLE_SECONDS),
                "recycle_seconds",
                minimum=60,
            ),
            lease_seconds=_as_int(
                value.get("lease_seconds", LEASE_SECONDS),
                "lease_seconds",
                minimum=30,
                maximum=600,
            ),
        )


class RemoteProjectService:
    """Transactional shared-project service for the Tunnel origin.

    Project metadata and mutable results live in SQLite. Uploaded source bytes
    are immutable from the application's perspective and each export is written
    to a new artifact. A process-local lock serializes the ReviewSession object
    with the corresponding database revision.
    """

    def __init__(
        self,
        config: RemoteRuntimeConfig,
        *,
        analyzer: Callable = analyze,
        start_workers: bool = True,
    ):
        self.config = config
        self.root = config.root
        self.db_root = self.root / "db"
        self.staging_root = self.root / "staging"
        self.projects_root = self.root / "projects"
        self.exports_root = self.root / "exports"
        self.recycle_root = self.root / "recycle"
        self.cache_root = self.root / "cache"
        self.logs_root = self.root / "logs"
        for directory in (
            self.db_root,
            self.staging_root,
            self.projects_root,
            self.exports_root,
            self.recycle_root,
            self.cache_root,
            self.logs_root,
            self.root / "config",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.database = self.db_root / "remote.sqlite3"
        self._lock = threading.RLock()
        self._sessions: dict[str, ReviewSession] = {}
        self._analyzer = analyzer
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._closed = threading.Event()
        self._worker: threading.Thread | None = None
        self._cleaner: threading.Thread | None = None
        self._initialize_database()
        self._recover_runs()
        if start_workers:
            self._worker = threading.Thread(
                target=self._worker_loop, name="saige-remote-analysis", daemon=True
            )
            self._worker.start()
            self._cleaner = threading.Thread(
                target=self._cleanup_loop, name="saige-remote-cleanup", daemon=True
            )
            self._cleaner.start()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    format TEXT NOT NULL,
                    primary_rel TEXT,
                    uploader TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_activity REAL NOT NULL,
                    revision INTEGER NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    storage_bytes INTEGER NOT NULL,
                    error TEXT,
                    recycled_at REAL,
                    recycle_path TEXT
                );
                CREATE INDEX IF NOT EXISTS projects_activity ON projects(state,last_activity);
                CREATE TABLE IF NOT EXISTS project_items (
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    item_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    original_label TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    analysis_state TEXT NOT NULL,
                    suggested_label TEXT,
                    suspicion_score REAL,
                    x REAL,
                    y REAL,
                    data_json TEXT NOT NULL,
                    PRIMARY KEY(project_id,item_id),
                    UNIQUE(project_id,ordinal)
                );
                CREATE INDEX IF NOT EXISTS project_items_filter
                    ON project_items(project_id,status,label,suspicion_score,ordinal);
                CREATE TABLE IF NOT EXISTS leases (
                    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
                    holder TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    acquired_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    project_name TEXT NOT NULL,
                    format TEXT NOT NULL,
                    primary_path TEXT,
                    total_size INTEGER NOT NULL,
                    file_count INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS upload_files (
                    upload_id TEXT NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
                    file_id TEXT NOT NULL,
                    logical_path TEXT NOT NULL,
                    role TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    expected_sha256 TEXT,
                    actual_sha256 TEXT,
                    PRIMARY KEY(upload_id,file_id),
                    UNIQUE(upload_id,logical_path)
                );
                CREATE TABLE IF NOT EXISTS upload_chunks (
                    upload_id TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    PRIMARY KEY(upload_id,file_id,chunk_index),
                    FOREIGN KEY(upload_id,file_id)
                        REFERENCES upload_files(upload_id,file_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS analysis_runs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    requested_by TEXT NOT NULL,
                    state TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    input_revision INTEGER NOT NULL,
                    progress REAL NOT NULL,
                    message TEXT NOT NULL,
                    error TEXT,
                    result_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL
                );
                CREATE INDEX IF NOT EXISTS analysis_queue ON analysis_runs(state,created_at);
                CREATE TABLE IF NOT EXISTS analysis_items (
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    item_id TEXT NOT NULL,
                    update_json TEXT NOT NULL,
                    PRIMARY KEY(project_id,item_id)
                );
                CREATE TABLE IF NOT EXISTS export_artifacts (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    actor TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    project_id TEXT,
                    revision INTEGER,
                    outcome TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                """
            )

    def _recover_runs(self) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE analysis_runs SET state='failed',error=?,message=?,updated_at=?,"
                "completed_at=? WHERE state='running'",
                ("服务重启中断了分析，请重新提交", "分析被服务重启中断", now, now),
            )
            rows = connection.execute(
                "SELECT id FROM analysis_runs WHERE state='queued' ORDER BY created_at,id"
            ).fetchall()
        for row in rows:
            self._queue.put(str(row["id"]))

    def _audit(
        self,
        connection: sqlite3.Connection,
        actor: str,
        action: str,
        project_id: str | None,
        revision: int | None,
        outcome: str = "ok",
        details: dict | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO audit_events(created_at,actor,action,project_id,revision,outcome,details_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (_now(), actor, action, project_id, revision, outcome, _json_text(details or {})),
        )

    def is_admin(self, actor: str) -> bool:
        return actor.lower() in self.config.admin_emails

    @staticmethod
    def _gpu_status() -> dict:
        try:
            import torch

            if not torch.cuda.is_available():
                return {"available": False, "device": "cpu", "name": None, "memory_bytes": 0}
            properties = torch.cuda.get_device_properties(0)
            return {
                "available": True,
                "device": "cuda",
                "name": properties.name,
                "memory_bytes": int(properties.total_memory),
            }
        except (ImportError, RuntimeError, OSError):
            return {"available": False, "device": "cpu", "name": None, "memory_bytes": 0}

    def bootstrap(self, actor: str) -> dict:
        config = AnalysisConfig(device="auto", model_access="local_only")
        return {
            "api_version": API_VERSION,
            "mode": "remote",
            "app_version": __version__,
            "user": {"email": actor, "is_admin": self.is_admin(actor)},
            "capabilities": {
                "local_path_picker": False,
                "source_overwrite": False,
                "shared_projects": True,
                "resumable_upload": True,
                "edit_leases": True,
                "download_exports": True,
            },
            "limits": {
                "chunk_size": CHUNK_SIZE,
                "max_project_size": self.config.max_project_size,
                "max_archive_entries": MAX_ARCHIVE_ENTRIES,
                "max_dataset_items": MAX_DATASET_ITEMS,
                "retention_seconds": self.config.retention_seconds,
                "recycle_seconds": self.config.recycle_seconds,
                "lease_seconds": self.config.lease_seconds,
            },
            "dependencies": dependency_status(),
            "model_status": model_status(config),
            "gpu": self._gpu_status(),
        }

    def _project_row(self, connection: sqlite3.Connection, project_id: str) -> sqlite3.Row:
        project_id = _safe_id(project_id, "project id")
        row = connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise LookupError("项目不存在")
        return row

    @staticmethod
    def _item_record(item) -> dict:
        return ReviewSession._public_item(item)

    def _insert_project_items(
        self, connection: sqlite3.Connection, project_id: str, session: ReviewSession
    ) -> None:
        connection.executemany(
            "INSERT INTO project_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                (
                    project_id,
                    item.id,
                    ordinal,
                    item.relative_path,
                    item.original_label,
                    item.label,
                    item.status,
                    item.analysis_state,
                    item.suggested_label,
                    item.suspicion_score,
                    item.x,
                    item.y,
                    _json_text(self._item_record(item)),
                )
                for ordinal, item in enumerate(session.dataset.items)
            ),
        )

    def _update_project_item(
        self, connection: sqlite3.Connection, project_id: str, item
    ) -> None:
        connection.execute(
            "UPDATE project_items SET label=?,status=?,analysis_state=?,suggested_label=?,"
            "suspicion_score=?,x=?,y=?,data_json=? WHERE project_id=? AND item_id=?",
            (
                item.label,
                item.status,
                item.analysis_state,
                item.suggested_label,
                item.suspicion_score,
                item.x,
                item.y,
                _json_text(self._item_record(item)),
                project_id,
                item.id,
            ),
        )

    def _public_project(self, row: sqlite3.Row, actor: str) -> dict:
        now = _now()
        with self._connect() as connection:
            lease = connection.execute(
                "SELECT holder,expires_at FROM leases WHERE project_id=? AND expires_at>?",
                (row["id"], now),
            ).fetchone()
            run = connection.execute(
                "SELECT state,progress,message FROM analysis_runs WHERE project_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
        return {
            "id": row["id"],
            "name": row["name"],
            "format": row["format"],
            "uploader": row["uploader"],
            "state": row["state"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_activity": row["last_activity"],
            "expires_at": row["last_activity"] + self.config.retention_seconds,
            "revision": row["revision"],
            "storage_bytes": row["storage_bytes"],
            "error": row["error"],
            "lock": (
                {"holder": lease["holder"], "expires_at": lease["expires_at"]}
                if lease else None
            ),
            "analysis": dict(run) if run else None,
            "permissions": {
                "view": row["state"] == "active",
                "edit": row["state"] == "active",
                "analyze": row["state"] == "active",
                "export": row["state"] == "active",
                "delete": self.is_admin(actor),
                "force_unlock": self.is_admin(actor),
                "restore": self.is_admin(actor) and row["state"] == "recycled",
            },
        }

    def list_projects(self, actor: str, *, recycled: bool = False) -> list[dict]:
        state = "recycled" if recycled else "active"
        if recycled and not self.is_admin(actor):
            raise PermissionError("仅管理员可查看回收区")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM projects WHERE state=? ORDER BY updated_at DESC,id", (state,)
            ).fetchall()
        return [self._public_project(row, actor) for row in rows]

    def audit_events(self, actor: str, *, limit: int = 200) -> dict:
        if not self.is_admin(actor):
            raise PermissionError("仅管理员可查看审计记录")
        limit = _as_int(limit, "limit", minimum=1, maximum=1000)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT created_at,actor,action,project_id,revision,outcome,details_json "
                "FROM audit_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return {
            "events": [
                {
                    "created_at": row["created_at"],
                    "actor": row["actor"],
                    "action": row["action"],
                    "project_id": row["project_id"],
                    "revision": row["revision"],
                    "outcome": row["outcome"],
                    "details": json.loads(row["details_json"]),
                }
                for row in rows
            ]
        }

    def _managed_bytes(self, connection: sqlite3.Connection) -> int:
        projects = connection.execute(
            "SELECT COALESCE(SUM(storage_bytes),0) FROM projects WHERE state IN ('active','recycled')"
        ).fetchone()[0]
        uploads = connection.execute(
            "SELECT COALESCE(SUM(total_size),0) FROM uploads WHERE state IN ('registering','uploading','needs-files','assembling')"
        ).fetchone()[0]
        exports = connection.execute(
            "SELECT COALESCE(SUM(size),0) FROM export_artifacts"
        ).fetchone()[0]
        return int(projects or 0) + int(uploads or 0) + int(exports or 0)

    def create_upload(self, actor: str, value: dict) -> dict:
        name = _safe_name(value.get("name"))
        project_format = str(value.get("format") or "")
        if project_format not in FORMATS:
            raise ValueError("不支持的项目格式")
        total_size = _as_int(
            value.get("total_size"), "total_size", minimum=1, maximum=self.config.max_project_size
        )
        file_count = _as_int(
            value.get("file_count"), "file_count", minimum=1, maximum=MAX_ARCHIVE_ENTRIES
        )
        primary_path_value = value.get("primary_path")
        primary_path = (
            _safe_logical_path(primary_path_value) if primary_path_value is not None else None
        )
        if project_format == "folder":
            primary_path = primary_path or "dataset"
        elif not primary_path:
            raise ValueError("项目文件上传缺少 primary_path")
        free = shutil.disk_usage(self.root).free
        if free < total_size * 2 + self.config.min_free_space:
            raise RuntimeError("服务器可用空间不足，无法安全预留上传与导入空间")
        upload_id = uuid.uuid4().hex
        now = _now()
        with self._lock, self._connect() as connection:
            if self._managed_bytes(connection) + total_size > self.config.max_managed_storage:
                raise RuntimeError("远程存储配额已满")
            connection.execute(
                "INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    upload_id,
                    actor,
                    name,
                    project_format,
                    primary_path,
                    total_size,
                    file_count,
                    "registering",
                    now,
                    now,
                ),
            )
            self._audit(connection, actor, "upload.create", None, None, details={"upload_id": upload_id})
            (self.staging_root / upload_id / "chunks").mkdir(parents=True, exist_ok=False)
        return self.get_upload(actor, upload_id)

    def _upload_row(self, connection: sqlite3.Connection, actor: str, upload_id: str) -> sqlite3.Row:
        upload_id = _safe_id(upload_id, "upload id")
        row = connection.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
        if row is None or row["owner"] != actor:
            raise LookupError("上传不存在")
        return row

    def register_files(self, actor: str, upload_id: str, files: object) -> dict:
        if not isinstance(files, list) or not files or len(files) > MAX_UPLOAD_BATCH:
            raise ValueError(f"files 必须是 1..{MAX_UPLOAD_BATCH} 项的数组")
        normalized = []
        for entry in files:
            if not isinstance(entry, dict):
                raise ValueError("文件清单项必须是对象")
            logical_path = _safe_logical_path(entry.get("logical_path"))
            size = _as_int(
                entry.get("size"), "file size", minimum=0, maximum=self.config.max_project_size
            )
            chunk_count = max(1, (size + CHUNK_SIZE - 1) // CHUNK_SIZE)
            declared_chunks = _as_int(entry.get("chunk_count", chunk_count), "chunk_count", minimum=1)
            if declared_chunks != chunk_count:
                raise ValueError(f"文件分块数量不正确：{logical_path}")
            expected = entry.get("sha256")
            if expected is not None and not SHA256_RE.fullmatch(str(expected)):
                raise ValueError(f"文件 SHA-256 无效：{logical_path}")
            role = str(entry.get("role") or "data")
            if role not in {"primary", "data"}:
                raise ValueError("文件 role 必须是 primary 或 data")
            file_id = hashlib.sha256(logical_path.casefold().encode("utf-8")).hexdigest()[:32]
            normalized.append((file_id, logical_path, role, size, chunk_count, expected))
        with self._lock, self._connect() as connection:
            upload = self._upload_row(connection, actor, upload_id)
            if upload["state"] not in {"registering", "uploading", "needs-files"}:
                raise RuntimeError("上传已不接受新文件")
            known = {
                row["file_id"]: row
                for row in connection.execute(
                    "SELECT * FROM upload_files WHERE upload_id=?", (upload_id,)
                ).fetchall()
            }
            additions = []
            for values in normalized:
                row = known.get(values[0])
                if row is None:
                    additions.append(values)
                    continue
                identity = (
                    row["logical_path"],
                    row["role"],
                    row["size"],
                    row["chunk_count"],
                    row["expected_sha256"],
                )
                if identity != values[1:]:
                    raise RuntimeError(
                        f"已登记文件与续传清单不一致：{values[1]}"
                    )
            existing = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(size),0) FROM upload_files WHERE upload_id=?",
                (upload_id,),
            ).fetchone()
            if int(existing[0]) + len(additions) > upload["file_count"]:
                raise ValueError("登记文件数量超过声明值")
            if int(existing[1]) + sum(value[3] for value in additions) > upload["total_size"]:
                raise ValueError("登记文件大小超过声明值")
            for values in additions:
                connection.execute(
                    "INSERT INTO upload_files(upload_id,file_id,logical_path,role,size,chunk_count,expected_sha256,actual_sha256) "
                    "VALUES(?,?,?,?,?,?,?,NULL)",
                    (upload_id, *values),
                )
                (self.staging_root / upload_id / "chunks" / values[0]).mkdir(exist_ok=False)
            connection.execute(
                "UPDATE uploads SET state='uploading',updated_at=?,error=NULL WHERE id=?",
                (_now(), upload_id),
            )
        return {
            "upload": self.get_upload(actor, upload_id),
            "files": [
                {"file_id": value[0], "logical_path": value[1]} for value in normalized
            ],
        }

    def get_upload(self, actor: str, upload_id: str) -> dict:
        with self._connect() as connection:
            row = self._upload_row(connection, actor, upload_id)
            registered = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(size),0) FROM upload_files WHERE upload_id=?",
                (upload_id,),
            ).fetchone()
            chunks = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(size),0) FROM upload_chunks WHERE upload_id=?",
                (upload_id,),
            ).fetchone()
        return {
            "id": row["id"],
            "name": row["project_name"],
            "format": row["format"],
            "primary_path": row["primary_path"],
            "total_size": row["total_size"],
            "file_count": row["file_count"],
            "registered_files": int(registered[0]),
            "registered_bytes": int(registered[1]),
            "received_chunks": int(chunks[0]),
            "received_bytes": int(chunks[1]),
            "state": row["state"],
            "error": row["error"],
            "chunk_size": CHUNK_SIZE,
        }

    def write_chunk(
        self,
        actor: str,
        upload_id: str,
        file_id: str,
        chunk_index: object,
        payload: bytes,
        expected_sha256: str,
    ) -> dict:
        file_id = _safe_id(file_id, "file id")
        index = _as_int(chunk_index, "chunk index", minimum=0)
        if not SHA256_RE.fullmatch(str(expected_sha256 or "")):
            raise ValueError("X-Chunk-SHA256 无效")
        actual = hashlib.sha256(payload).hexdigest()
        if not hmac.compare_digest(actual, expected_sha256):
            raise ValueError("分块 SHA-256 不匹配")
        with self._lock, self._connect() as connection:
            upload = self._upload_row(connection, actor, upload_id)
            if upload["state"] not in {"uploading", "needs-files"}:
                raise RuntimeError("上传已不接受分块")
            file_row = connection.execute(
                "SELECT * FROM upload_files WHERE upload_id=? AND file_id=?",
                (upload_id, file_id),
            ).fetchone()
            if file_row is None or index >= file_row["chunk_count"]:
                raise ValueError("文件或分块索引无效")
            expected_size = (
                CHUNK_SIZE
                if index < file_row["chunk_count"] - 1
                else file_row["size"] - CHUNK_SIZE * (file_row["chunk_count"] - 1)
            )
            if len(payload) != expected_size:
                raise ValueError("分块大小与位置不符")
            existing = connection.execute(
                "SELECT size,sha256 FROM upload_chunks WHERE upload_id=? AND file_id=? AND chunk_index=?",
                (upload_id, file_id, index),
            ).fetchone()
            target = self.staging_root / upload_id / "chunks" / file_id / f"{index:08d}.part"
            if existing:
                if existing["size"] == len(payload) and existing["sha256"] == actual and target.is_file():
                    return self.get_upload(actor, upload_id)
                raise RuntimeError("同一分块已存在但内容不一致")
            try:
                with target.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                connection.execute(
                    "INSERT INTO upload_chunks VALUES(?,?,?,?,?)",
                    (upload_id, file_id, index, len(payload), actual),
                )
                connection.execute(
                    "UPDATE uploads SET state='uploading',updated_at=?,error=NULL WHERE id=?",
                    (_now(), upload_id),
                )
            except Exception:
                target.unlink(missing_ok=True)
                raise
        return self.get_upload(actor, upload_id)

    def _assemble_upload(self, upload_id: str, files: list[sqlite3.Row]) -> Path:
        upload_root = self.staging_root / upload_id
        assembled = upload_root / "assembled"
        if assembled.exists():
            shutil.rmtree(assembled)
        assembled.mkdir()
        for file_row in files:
            target = assembled.joinpath(*PurePosixPath(file_row["logical_path"]).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            written = 0
            with target.open("xb") as output:
                for index in range(file_row["chunk_count"]):
                    part = upload_root / "chunks" / file_row["file_id"] / f"{index:08d}.part"
                    with part.open("rb") as source:
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            output.write(block)
                            digest.update(block)
                            written += len(block)
                output.flush()
                os.fsync(output.fileno())
            actual = digest.hexdigest()
            if written != file_row["size"]:
                raise ValueError(f"文件组装大小不匹配：{file_row['logical_path']}")
            expected = file_row["expected_sha256"]
            if expected and not hmac.compare_digest(expected, actual):
                raise ValueError(f"文件 SHA-256 不匹配：{file_row['logical_path']}")
            with self._connect() as connection:
                connection.execute(
                    "UPDATE upload_files SET actual_sha256=? WHERE upload_id=? AND file_id=?",
                    (actual, upload_id, file_row["file_id"]),
                )
        return assembled

    @staticmethod
    def _candidate_image_map(source_root: Path) -> tuple[dict[str, list[Path]], list[Path]]:
        by_name: dict[str, list[Path]] = {}
        images: list[Path] = []
        for path in source_root.rglob("*"):
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                images.append(path)
                by_name.setdefault(path.name.casefold(), []).append(path)
        return by_name, images

    def _map_external_images(self, dataset, primary: Path, source_root: Path) -> tuple[list[str], dict]:
        by_name, images = self._candidate_image_map(source_root)
        mapped: dict[str, str] = {}
        unresolved: list[str] = []
        for item in dataset.items:
            original = item.relative_path
            normalized = original.replace("\\", "/").strip("/").casefold()
            suffix_matches = [
                path
                for path in images
                if path.relative_to(source_root).as_posix().casefold().endswith(normalized)
                or normalized.endswith(path.relative_to(source_root).as_posix().casefold())
            ]
            candidates = suffix_matches or by_name.get(Path(original).name.casefold(), [])
            unique = {str(path.resolve()).casefold(): path for path in candidates}
            if len(unique) != 1:
                if original not in unresolved and len(unresolved) < 100:
                    unresolved.append(original)
                continue
            candidate = next(iter(unique.values()))
            relative = os.path.relpath(candidate, primary.parent)
            item.relative_path = relative
            item.metadata["remote_original_path"] = original
            mapped[original] = candidate.relative_to(source_root).as_posix()
        if mapped:
            dataset.metadata["allowed_preview_roots"] = [str(source_root.resolve())]
        return unresolved, mapped

    def complete_upload(self, actor: str, upload_id: str) -> dict:
        with self._lock, self._connect() as connection:
            upload = self._upload_row(connection, actor, upload_id)
            if upload["state"] not in {"uploading", "needs-files"}:
                raise RuntimeError("上传当前状态不能完成")
            files = connection.execute(
                "SELECT * FROM upload_files WHERE upload_id=? ORDER BY logical_path", (upload_id,)
            ).fetchall()
            registered_size = sum(int(row["size"]) for row in files)
            if len(files) != upload["file_count"] or registered_size != upload["total_size"]:
                raise ValueError("文件清单尚未完整登记")
            for file_row in files:
                received = connection.execute(
                    "SELECT COUNT(*) FROM upload_chunks WHERE upload_id=? AND file_id=?",
                    (upload_id, file_row["file_id"]),
                ).fetchone()[0]
                if received != file_row["chunk_count"]:
                    raise ValueError(f"文件尚未上传完整：{file_row['logical_path']}")
            connection.execute(
                "UPDATE uploads SET state='assembling',updated_at=?,error=NULL WHERE id=?",
                (_now(), upload_id),
            )
        project_dir = None
        committed = False
        try:
            assembled = self._assemble_upload(upload_id, list(files))
            project_format = str(upload["format"])
            primary_rel = str(upload["primary_path"] or "")
            primary = (
                assembled.joinpath(*PurePosixPath(primary_rel).parts)
                if project_format != "folder"
                else assembled.joinpath(*PurePosixPath(primary_rel).parts)
            )
            if not primary.exists():
                raise ValueError("找不到声明的主项目文件或数据集根目录")
            dataset = load_source(primary)
            expected_source = {
                "visionproj": "visionproj:",
                "srproj": "srproj:",
                "saige-json": "saige-json:",
                "folder": "folder",
            }[project_format]
            if not (
                dataset.source_type == expected_source
                or dataset.source_type.startswith(expected_source)
            ):
                raise ValueError(
                    f"上传内容与声明格式不一致：声明 {project_format}，实际 {dataset.source_type}"
                )
            mapping = {}
            if project_format in {"srproj", "saige-json"}:
                unresolved, mapping = self._map_external_images(dataset, primary, assembled)
                if unresolved:
                    with self._connect() as connection:
                        connection.execute(
                            "UPDATE uploads SET state='needs-files',updated_at=?,error=? WHERE id=?",
                            (_now(), "仍有图像引用无法映射", upload_id),
                        )
                    return {"upload": self.get_upload(actor, upload_id), "unresolved": unresolved}
            if not dataset.items:
                raise ValueError("项目中没有可复查的已标注样本")
            project_id = uuid.uuid4().hex
            project_dir = self.projects_root / project_id
            project_dir.mkdir(exist_ok=False)
            source_target = project_dir / "source"
            os.replace(assembled, source_target)
            if mapping:
                _atomic_json(project_dir / "path-mapping.json", mapping)
            source_path = (
                source_target.joinpath(*PurePosixPath(primary_rel).parts)
                if project_format != "folder"
                else source_target.joinpath(*PurePosixPath(primary_rel).parts)
            )
            loaded = load_source(source_path)
            if project_format in {"srproj", "saige-json"}:
                unresolved, _ = self._map_external_images(loaded, source_path, source_target)
                if unresolved:
                    raise RuntimeError("项目提交后图像映射校验失败")
            session = ReviewSession(loaded)
            _atomic_json(project_dir / "source-manifest.json", _source_manifest(source_target))
            for immutable in source_target.rglob("*"):
                if immutable.is_file():
                    os.chmod(immutable, stat.S_IREAD)
            source_digest = loaded.source_hash
            now = _now()
            storage_bytes = sum(int(row["size"]) for row in files)
            with self._lock, self._connect() as connection:
                connection.execute(
                    "INSERT INTO projects VALUES(?,?,?,?,?,'active',?,?,?,?,?,?,NULL,NULL,NULL)",
                    (
                        project_id,
                        upload["project_name"],
                        project_format,
                        primary_rel,
                        actor,
                        now,
                        now,
                        now,
                        0,
                        source_digest,
                        storage_bytes,
                    ),
                )
                self._insert_project_items(connection, project_id, session)
                connection.execute("DELETE FROM uploads WHERE id=?", (upload_id,))
                self._audit(connection, actor, "project.import", project_id, 0)
                self._sessions[project_id] = session
            committed = True
            shutil.rmtree(self.staging_root / upload_id, ignore_errors=True)
            return {"project": self.get_project(actor, project_id), "unresolved": []}
        except Exception as error:
            if project_dir is not None and project_dir.exists() and not committed:
                self._remove_tree(project_dir)
            if project_dir is not None and not committed:
                self._sessions.pop(project_dir.name, None)
            with self._connect() as connection:
                row = connection.execute("SELECT id FROM uploads WHERE id=?", (upload_id,)).fetchone()
                if row:
                    connection.execute(
                        "UPDATE uploads SET state='uploading',updated_at=?,error=? WHERE id=?",
                        (_now(), str(error)[:500], upload_id),
                    )
            raise

    def get_project(self, actor: str, project_id: str) -> dict:
        with self._connect() as connection:
            row = self._project_row(connection, project_id)
        return self._public_project(row, actor)

    def _source_path(self, row: sqlite3.Row) -> Path:
        root = self.projects_root / row["id"] / "source"
        if row["format"] == "folder":
            return root.joinpath(*PurePosixPath(row["primary_rel"] or "dataset").parts)
        return root.joinpath(*PurePosixPath(row["primary_rel"]).parts)

    def _load_session_locked(self, project_id: str) -> ReviewSession:
        cached = self._sessions.get(project_id)
        if cached is not None:
            return cached
        with self._connect() as connection:
            row = self._project_row(connection, project_id)
            if row["state"] != "active":
                raise RuntimeError("项目当前不可编辑")
            source = self._source_path(row)
            updates = connection.execute(
                "SELECT update_json FROM analysis_items WHERE project_id=? ORDER BY item_id",
                (project_id,),
            ).fetchall()
        dataset = load_source(source)
        if row["format"] in {"srproj", "saige-json"}:
            unresolved, _ = self._map_external_images(dataset, source, source.parents[1])
            if unresolved:
                raise RuntimeError("远程图像映射不完整，请重新上传项目")
        session = ReviewSession(dataset)
        state_path = self.projects_root / project_id / "session" / "state.json"
        if state_path.exists():
            session.restore(state_path)
        if updates:
            values = [json.loads(item["update_json"]) for item in updates]
            if len(values) == len(dataset.items):
                session.stage_analysis_updates(values)
        self._sessions[project_id] = session
        return session

    def _touch(self, project_id: str, *, force: bool = False) -> None:
        now = _now()
        with self._connect() as connection:
            if force:
                connection.execute(
                    "UPDATE projects SET last_activity=?,updated_at=? WHERE id=? AND state='active'",
                    (now, now, project_id),
                )
            else:
                connection.execute(
                    "UPDATE projects SET last_activity=?,updated_at=? WHERE id=? AND state='active' "
                    "AND last_activity<?",
                    (now, now, project_id, now - VIEW_TOUCH_INTERVAL),
                )

    def _latest_job(self, project_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_runs WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if row is None:
                return {"state": "idle", "progress": 0.0, "message": ""}
            queued_before = 0
            if row["state"] == "queued":
                queued_before = connection.execute(
                    "SELECT COUNT(*) FROM analysis_runs WHERE state='queued' AND "
                    "(created_at<? OR (created_at=? AND id<?))",
                    (row["created_at"], row["created_at"], row["id"]),
                ).fetchone()[0]
        result = json.loads(row["result_json"]) if row["result_json"] else None
        return {
            "id": row["id"],
            "state": row["state"],
            "progress": row["progress"],
            "message": row["message"],
            "error": row["error"],
            "result": result,
            "queue_position": queued_before + 1 if row["state"] == "queued" else None,
        }

    def session_payload(
        self, actor: str, project_id: str, *, touch: bool = True, compact: bool = False
    ) -> dict:
        project_id = _safe_id(project_id, "project id")
        with self._lock:
            session = self._load_session_locked(project_id)
            with self._connect() as connection:
                row = self._project_row(connection, project_id)
                lease = connection.execute(
                    "SELECT holder,expires_at FROM leases WHERE project_id=? AND expires_at>?",
                    (project_id, _now()),
                ).fetchone()
            payload = session.payload()
            payload["item_count"] = len(payload["items"])
            if compact:
                payload["items"] = []
            payload["source"] = f"remote://{project_id}"
            payload["name"] = row["name"]
            for item in payload["items"]:
                item["preview_url"] = f"/api/projects/{project_id}/preview/{item['id']}"
            payload.update(
                {
                    "api_version": API_VERSION,
                    "app_version": __version__,
                    "remote_mode": True,
                    "project_id": project_id,
                    "project_revision": row["revision"],
                    "project_format": row["format"],
                    "read_only": lease is None or lease["holder"] != actor,
                    "lock": (
                        {"holder": lease["holder"], "expires_at": lease["expires_at"]}
                        if lease else None
                    ),
                    "permissions": self._public_project(row, actor)["permissions"],
                    "recent": [],
                    "dependencies": dependency_status(),
                    "analysis_config": asdict(
                        AnalysisConfig(device="auto", model_access="local_only")
                    ),
                    "model_status": model_status(
                        AnalysisConfig(device="auto", model_access="local_only")
                    ),
                    "job": self._latest_job(project_id),
                    "requires_source_reload": False,
                }
            )
        if touch:
            self._touch(project_id)
        return payload

    def _item_filter(
        self,
        project_id: str,
        *,
        search: str = "",
        label: str = "all",
        status: str = "all",
        min_score: float = 0.0,
    ) -> tuple[str, list]:
        clauses = ["project_id=?"]
        values: list = [project_id]
        search = str(search or "").strip().casefold()
        if len(search) > 200:
            raise ValueError("搜索文本过长")
        if search:
            clauses.append(
                "LOWER(relative_path || ' ' || label || ' ' || COALESCE(suggested_label,'')) LIKE ? ESCAPE '\\'"
            )
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            values.append(f"%{escaped}%")
        if label != "all":
            clauses.append("label=?")
            values.append(str(label))
        allowed_status = {"all", "pending", "correct", "modified", "uncertain", "skipped"}
        if status not in allowed_status:
            raise ValueError("状态筛选无效")
        if status != "all":
            clauses.append("status=?")
            values.append(status)
        try:
            minimum = float(min_score)
        except (TypeError, ValueError) as error:
            raise ValueError("最低分数无效") from error
        if not 0 <= minimum <= 100:
            raise ValueError("最低分数无效")
        if minimum > 0:
            clauses.append("suspicion_score>=?")
            values.append(minimum)
        return " AND ".join(clauses), values

    def list_items(
        self,
        actor: str,
        project_id: str,
        *,
        offset: int = 0,
        limit: int = 400,
        search: str = "",
        label: str = "all",
        status: str = "all",
        min_score: float = 0.0,
    ) -> dict:
        self.get_project(actor, project_id)
        offset = _as_int(offset, "offset", minimum=0)
        limit = _as_int(limit, "limit", minimum=1, maximum=500)
        where, values = self._item_filter(
            project_id,
            search=search,
            label=label,
            status=status,
            min_score=min_score,
        )
        with self._connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM project_items WHERE {where}", values
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT data_json FROM project_items WHERE {where} "
                "ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,"
                "CASE WHEN suspicion_score IS NULL THEN 1 ELSE 0 END,suspicion_score DESC,ordinal "
                "LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
        items = [json.loads(row["data_json"]) for row in rows]
        for item in items:
            item["preview_url"] = f"/api/projects/{project_id}/preview/{item['id']}"
        return {"items": items, "total": int(total), "offset": offset, "limit": limit}

    def feature_sample(
        self,
        actor: str,
        project_id: str,
        *,
        limit: int = 20_000,
        **filters,
    ) -> dict:
        self.get_project(actor, project_id)
        limit = _as_int(limit, "limit", minimum=1, maximum=20_000)
        where, values = self._item_filter(project_id, **filters)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT item_id,label,status,data_json FROM project_items WHERE {where} "
                "AND x IS NOT NULL AND y IS NOT NULL ORDER BY ordinal",
                values,
            ).fetchall()
        total = len(rows)
        if total > limit:
            groups: dict[tuple[str, str], list] = {}
            for row in rows:
                groups.setdefault((row["label"], row["status"]), []).append(row)
            ordered = sorted(groups)
            quotas = {key: 1 for key in ordered}
            remaining = max(0, limit - len(ordered))
            assigned = 0
            fractions = []
            for key in ordered:
                exact = remaining * len(groups[key]) / total
                extra = min(len(groups[key]) - 1, int(exact))
                quotas[key] += extra
                assigned += extra
                fractions.append((exact - int(exact), key))
            spare = remaining - assigned
            for _, key in sorted(fractions, reverse=True):
                if spare <= 0:
                    break
                if quotas[key] < len(groups[key]):
                    quotas[key] += 1
                    spare -= 1
            rows = [
                row
                for key in ordered
                for row in sorted(
                    groups[key],
                    key=lambda value: hashlib.sha256(
                        value["item_id"].encode("utf-8")
                    ).digest(),
                )[: quotas[key]]
            ][:limit]
        items = [json.loads(row["data_json"]) for row in rows]
        for item in items:
            item["preview_url"] = f"/api/projects/{project_id}/preview/{item['id']}"
        return {"items": items, "total": total, "sampled": total > len(items)}

    def references(
        self, actor: str, project_id: str, label: str, *, exclude: str = "", limit: int = 3
    ) -> dict:
        self.get_project(actor, project_id)
        limit = _as_int(limit, "limit", minimum=1, maximum=12)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT data_json FROM project_items WHERE project_id=? AND label=? AND item_id<>? "
                "ORDER BY CASE status WHEN 'correct' THEN 0 WHEN 'modified' THEN 1 ELSE 2 END,"
                "CASE WHEN suspicion_score IS NULL THEN 1 ELSE 0 END,suspicion_score,ordinal LIMIT ?",
                (project_id, str(label), str(exclude), limit),
            ).fetchall()
        items = [json.loads(row["data_json"]) for row in rows]
        for item in items:
            item["preview_url"] = f"/api/projects/{project_id}/preview/{item['id']}"
        return {"items": items}

    def acquire_lease(self, actor: str, project_id: str, *, force: bool = False) -> dict:
        now = _now()
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        with self._lock, self._connect() as connection:
            project = self._project_row(connection, project_id)
            if project["state"] != "active":
                raise RuntimeError("项目当前不可编辑")
            existing = connection.execute(
                "SELECT * FROM leases WHERE project_id=?", (project_id,)
            ).fetchone()
            if existing and existing["expires_at"] > now and existing["holder"] != actor:
                if not (force and self.is_admin(actor)):
                    raise RuntimeError(f"项目正在由 {existing['holder']} 编辑")
            connection.execute(
                "INSERT INTO leases VALUES(?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
                "holder=excluded.holder,token_hash=excluded.token_hash,acquired_at=excluded.acquired_at,"
                "expires_at=excluded.expires_at",
                (project_id, actor, token_hash, now, now + self.config.lease_seconds),
            )
            self._audit(connection, actor, "lock.acquire", project_id, project["revision"], details={"force": force})
        return {"token": raw_token, "holder": actor, "expires_at": now + self.config.lease_seconds}

    def heartbeat_lease(self, actor: str, project_id: str, token: str) -> dict:
        now = _now()
        token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE leases SET expires_at=? WHERE project_id=? AND holder=? AND token_hash=? "
                "AND expires_at>?",
                (now + self.config.lease_seconds, project_id, actor, token_hash, now),
            )
            if result.rowcount != 1:
                raise RuntimeError("编辑锁已失效，请重新取得编辑权")
        return {"holder": actor, "expires_at": now + self.config.lease_seconds}

    def release_lease(self, actor: str, project_id: str, token: str, *, force: bool = False) -> None:
        token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        with self._lock, self._connect() as connection:
            project = self._project_row(connection, project_id)
            if force and self.is_admin(actor):
                result = connection.execute("DELETE FROM leases WHERE project_id=?", (project_id,))
            else:
                result = connection.execute(
                    "DELETE FROM leases WHERE project_id=? AND holder=? AND token_hash=?",
                    (project_id, actor, token_hash),
                )
            if result.rowcount == 0:
                raise RuntimeError("没有可释放的编辑锁")
            self._audit(connection, actor, "lock.release", project_id, project["revision"], details={"force": force})

    def _require_lease(
        self,
        connection: sqlite3.Connection,
        actor: str,
        project_id: str,
        token: str,
        expected_revision: int,
        *,
        allow_busy: bool = False,
    ) -> sqlite3.Row:
        project = self._project_row(connection, project_id)
        if project["state"] != "active":
            raise RuntimeError("项目当前不可修改")
        if int(project["revision"]) != int(expected_revision):
            raise RuntimeError("项目版本已变化，请刷新后重试")
        token_hash = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
        lease = connection.execute(
            "SELECT * FROM leases WHERE project_id=? AND holder=? AND token_hash=? AND expires_at>?",
            (project_id, actor, token_hash, _now()),
        ).fetchone()
        if lease is None:
            raise PermissionError("需要有效的项目编辑锁")
        if not allow_busy:
            busy = connection.execute(
                "SELECT 1 FROM analysis_runs WHERE project_id=? AND state IN ('queued','running')",
                (project_id,),
            ).fetchone()
            if busy:
                raise RuntimeError("项目正在排队或分析，暂时不能修改")
        return project

    def mutate(
        self,
        actor: str,
        project_id: str,
        token: str,
        expected_revision: int,
        operation: str,
        value: dict | None = None,
    ) -> dict:
        mutation = None
        session = None
        state_path = self.projects_root / project_id / "session" / "state.json"
        try:
            with self._lock, self._connect() as connection:
                project = self._require_lease(
                    connection, actor, project_id, token, expected_revision
                )
                session = self._load_session_locked(project_id)
                if operation == "update":
                    value = value or {}
                    item_id = str(value.get("id"))
                    mutation = session.stage_update(
                        item_id, label=value.get("label"), status=value.get("status")
                    )
                elif operation == "undo":
                    mutation = session.stage_undo()
                    item_id = mutation.command.item_id if mutation else None
                elif operation == "redo":
                    mutation = session.stage_redo()
                    item_id = mutation.command.item_id if mutation else None
                else:
                    raise ValueError("未知复查操作")
                if mutation is None:
                    response = session.mutation_payload(item_id)
                    response["project_revision"] = project["revision"]
                    return response
                session.save(state_path)
                self._update_project_item(
                    connection, project_id, session._items_by_id[str(item_id)]
                )
                revision = int(project["revision"]) + 1
                connection.execute(
                    "UPDATE projects SET revision=?,updated_at=?,last_activity=? WHERE id=?",
                    (revision, _now(), _now(), project_id),
                )
                self._audit(connection, actor, f"review.{operation}", project_id, revision)
                response = session.mutation_payload(item_id)
                response["project_revision"] = revision
                return response
        except Exception:
            if mutation is not None and session is not None:
                session.rollback_review(mutation)
                try:
                    session.save(state_path)
                except Exception:
                    self._sessions.pop(project_id, None)
            raise

    def submit_analysis(
        self,
        actor: str,
        project_id: str,
        token: str,
        expected_revision: int,
        config_value: dict,
    ) -> dict:
        supplied = dict(config_value or {})
        supplied["device"] = "auto"
        supplied["model_access"] = "local_only"
        config = AnalysisConfig.from_dict(supplied)
        run_id = uuid.uuid4().hex
        now = _now()
        with self._lock, self._connect() as connection:
            project = self._require_lease(
                connection, actor, project_id, token, expected_revision
            )
            existing = connection.execute(
                "SELECT 1 FROM analysis_runs WHERE project_id=? AND state IN ('queued','running')",
                (project_id,),
            ).fetchone()
            if existing:
                raise RuntimeError("项目已有排队或运行中的分析")
            connection.execute(
                "INSERT INTO analysis_runs VALUES(?,?,?,'queued',?,?,0,?,NULL,NULL,?,?,NULL,NULL)",
                (
                    run_id,
                    project_id,
                    actor,
                    _json_text(asdict(config)),
                    project["revision"],
                    "等待 GPU 分析队列",
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE projects SET updated_at=?,last_activity=? WHERE id=?",
                (now, now, project_id),
            )
            self._audit(connection, actor, "analysis.queue", project_id, project["revision"], details={"run_id": run_id})
        self._queue.put(run_id)
        return self._latest_job(project_id)

    def _set_run_progress(self, run_id: str, progress: float, message: str) -> None:
        now = _now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state,updated_at FROM analysis_runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is None:
                return
            if row["state"] == "cancelled":
                raise RuntimeError("分析已取消")
            if row["state"] != "running" or (now - row["updated_at"] < 0.4 and progress < 1):
                return
            connection.execute(
                "UPDATE analysis_runs SET progress=?,message=?,updated_at=? WHERE id=? AND state='running'",
                (max(0.0, min(float(progress), 1.0)), str(message)[:500], now, run_id),
            )

    def _worker_loop(self) -> None:
        while not self._closed.is_set():
            run_id = self._queue.get()
            try:
                if run_id is None:
                    return
                try:
                    self._run_analysis(run_id)
                except Exception as error:
                    now = _now()
                    with self._connect() as connection:
                        connection.execute(
                            "UPDATE analysis_runs SET state='failed',error=?,message='分析失败',"
                            "updated_at=?,completed_at=? WHERE id=? AND state IN ('queued','running')",
                            (str(error)[:1000], now, now, run_id),
                        )
                    print(f"remote analysis worker recovered from {type(error).__name__}: {error}")
            finally:
                self._queue.task_done()

    def _run_analysis(self, run_id: str) -> None:
        with self._lock, self._connect() as connection:
            run = connection.execute("SELECT * FROM analysis_runs WHERE id=?", (run_id,)).fetchone()
            if run is None or run["state"] != "queued":
                return
            now = _now()
            connection.execute(
                "UPDATE analysis_runs SET state='running',message='正在加载项目',updated_at=?,started_at=? WHERE id=?",
                (now, now, run_id),
            )
            project_id = str(run["project_id"])
            config = AnalysisConfig.from_dict(json.loads(run["config_json"]))
            session = self._load_session_locked(project_id)
        applied_mutation = None
        try:
            result = self._analyzer(
                session.dataset,
                config,
                self.projects_root / project_id / "analysis" / "cache",
                lambda value, message: self._set_run_progress(run_id, value, message),
            )
            updates = result.get("item_updates") if isinstance(result, dict) else None
            public_result = {
                key: value for key, value in (result or {}).items() if key != "item_updates"
            }
            with self._lock, self._connect() as connection:
                run = connection.execute(
                    "SELECT * FROM analysis_runs WHERE id=?", (run_id,)
                ).fetchone()
                project = self._project_row(connection, project_id)
                if run is None or run["state"] != "running":
                    return
                if project["revision"] != run["input_revision"]:
                    raise RuntimeError("分析期间项目版本已变化，结果未应用")
                applied_mutation = session.stage_analysis_updates(updates)
                revision = int(project["revision"]) + 1
                try:
                    connection.execute("DELETE FROM analysis_items WHERE project_id=?", (project_id,))
                    connection.executemany(
                        "INSERT INTO analysis_items VALUES(?,?,?)",
                        ((project_id, str(update["id"]), _json_text(update)) for update in updates),
                    )
                    for update in updates:
                        self._update_project_item(
                            connection,
                            project_id,
                            session._items_by_id[str(update["id"])],
                        )
                    now = _now()
                    connection.execute(
                        "UPDATE projects SET revision=?,updated_at=?,last_activity=? WHERE id=?",
                        (revision, now, now, project_id),
                    )
                    connection.execute(
                        "UPDATE analysis_runs SET state='completed',progress=1,message='分析完成',"
                        "result_json=?,updated_at=?,completed_at=? WHERE id=?",
                        (_json_text(public_result), now, now, run_id),
                    )
                    self._audit(connection, run["requested_by"], "analysis.complete", project_id, revision, details={"run_id": run_id})
                except Exception:
                    session.rollback_analysis(applied_mutation)
                    applied_mutation = None
                    raise
        except Exception as error:
            if applied_mutation is not None:
                session.rollback_analysis(applied_mutation)
                applied_mutation = None
            now = _now()
            with self._connect() as connection:
                connection.execute(
                    "UPDATE analysis_runs SET state='failed',error=?,message='分析失败',updated_at=?,completed_at=? "
                    "WHERE id=? AND state IN ('queued','running')",
                    (str(error)[:1000], now, now, run_id),
                )
                self._audit(
                    connection,
                    "system",
                    "analysis.failed",
                    project_id,
                    None,
                    "failed",
                    {"run_id": run_id, "error_type": type(error).__name__},
                )
        finally:
            gc.collect()
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except (ImportError, RuntimeError):
                pass

    def job_payload(self, actor: str, project_id: str) -> dict:
        self.get_project(actor, project_id)
        return {"job": self._latest_job(project_id)}

    def cancel_analysis(
        self,
        actor: str,
        project_id: str,
        token: str,
        expected_revision: int,
        run_id: str,
    ) -> dict:
        run_id = _safe_id(run_id, "analysis run id")
        with self._lock, self._connect() as connection:
            project = self._require_lease(
                connection,
                actor,
                project_id,
                token,
                expected_revision,
                allow_busy=True,
            )
            run = connection.execute(
                "SELECT state FROM analysis_runs WHERE id=? AND project_id=?",
                (run_id, project_id),
            ).fetchone()
            if run is None:
                raise LookupError("分析任务不存在")
            if run["state"] not in {"queued", "running"}:
                raise RuntimeError("分析任务已经结束")
            now = _now()
            connection.execute(
                "UPDATE analysis_runs SET state='cancelled',message='已取消',updated_at=?,"
                "completed_at=? WHERE id=?",
                (now, now, run_id),
            )
            self._audit(
                connection,
                actor,
                "analysis.cancel",
                project_id,
                project["revision"],
                details={"run_id": run_id},
            )
        return self._latest_job(project_id)

    def preview(
        self,
        actor: str,
        project_id: str,
        item_id: str,
        mode: str,
        show_contours: bool,
    ):
        del actor
        with self._lock:
            session = self._load_session_locked(project_id)
            item = session._item(str(item_id))
            if item is None:
                raise LookupError("复查项不存在")
            return render_preview(session.dataset, item, mode, show_contours=show_contours)

    def _artifact_path(self, project_id: str, artifact_id: str, suffix: str) -> Path:
        target = self.exports_root / project_id
        target.mkdir(parents=True, exist_ok=True)
        return target / f"{artifact_id}{suffix}"

    @staticmethod
    def _zip_tree(
        source: Path,
        target: Path,
        *,
        replacement: tuple[Path, Path] | None = None,
        extras: dict[str, bytes] | None = None,
    ) -> None:
        extras = dict(extras or {})
        readme_name = "SAIGE_EXPORT_README.txt"
        manifest_name = "SAIGE_EXPORT_SHA256SUMS.txt"
        extras.setdefault(
            readme_name,
            (
                "Saige Label Reviewer v0.1.0 remote export\r\n"
                "The server original was not overwritten. Verify files with "
                "SAIGE_EXPORT_SHA256SUMS.txt before import.\r\n"
            ).encode("utf-8"),
        )
        expected: dict[str, str] = {}
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for path in sorted(source.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(source)
                name = relative.as_posix()
                if name in extras or name == manifest_name:
                    raise ValueError(f"项目包含远程导出保留文件名：{name}")
                actual = replacement[1] if replacement and relative == replacement[0] else path
                archive.write(actual, name)
                expected[name] = _stream_sha256(actual)
            for name, payload in sorted(extras.items()):
                safe_name = _safe_logical_path(name)
                if safe_name in expected or safe_name == manifest_name:
                    raise ValueError(f"远程导出附加文件名冲突：{safe_name}")
                archive.writestr(safe_name, payload)
                expected[safe_name] = hashlib.sha256(payload).hexdigest()
            manifest = "".join(
                f"{digest}  {name}\n" for name, digest in sorted(expected.items())
            ).encode("utf-8")
            archive.writestr(manifest_name, manifest)
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != set(expected) | {manifest_name}:
                raise RuntimeError("导出 ZIP 文件清单复读不一致")
            for name, digest in expected.items():
                if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                    raise RuntimeError(f"导出 ZIP 内容复读不一致：{name}")
            if archive.read(manifest_name) != manifest:
                raise RuntimeError("导出 SHA-256 清单复读不一致")

    def create_export(self, actor: str, project_id: str, *, session_only: bool = False) -> dict:
        with self._lock, self._connect() as connection:
            project = self._project_row(connection, project_id)
            if project["state"] != "active":
                raise RuntimeError("项目当前不可导出")
            busy = connection.execute(
                "SELECT 1 FROM analysis_runs WHERE project_id=? AND state IN ('queued','running')",
                (project_id,),
            ).fetchone()
            if busy:
                raise RuntimeError("项目正在排队或分析，暂时不能导出")
            estimate = 1024 * 1024 if session_only else max(1024 * 1024, int(project["storage_bytes"]))
            if shutil.disk_usage(self.root).free < self.config.min_free_space + estimate * 2:
                raise RuntimeError("服务器可用空间不足，无法安全生成和校验导出")
            if self._managed_bytes(connection) + estimate > self.config.max_managed_storage:
                raise RuntimeError("远程存储配额不足，无法生成导出")
            session = self._load_session_locked(project_id)
            source_root = self.projects_root / project_id / "source"
            source_manifest_path = self.projects_root / project_id / "source-manifest.json"
            if source_manifest_path.is_file():
                expected_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
                if not hmac.compare_digest(
                    _json_text(_source_manifest(source_root)), _json_text(expected_manifest)
                ):
                    raise RuntimeError("服务器只读原件完整性校验失败，已停止导出")
            elif load_source(self._source_path(project)).source_hash != project["source_sha256"]:
                raise RuntimeError("服务器只读原件摘要已变化，已停止导出")
            artifact_id = uuid.uuid4().hex
            target: Path | None = None
            work: Path | None = None
            try:
                if session_only:
                    target = self._artifact_path(project_id, artifact_id, ".json")
                    _atomic_json(target, session.export_payload())
                    filename = f"{project['name']}-review-session.json"
                else:
                    changed = any(item.label != item.original_label for item in session.dataset.items)
                    corrected: Path | None = None
                    if changed:
                        work = self.projects_root / project_id / "export-work"
                        result = export_corrected(session, work, overwrite=False)
                        corrected = Path(result["output"])
                    if project["format"] == "visionproj":
                        target = self._artifact_path(project_id, artifact_id, ".visionproj")
                        source = corrected or self._source_path(project)
                        shutil.copy2(source, target)
                        if _stream_sha256(target) != _stream_sha256(source):
                            raise RuntimeError("visionproj 导出复读校验失败")
                        filename = f"{project['name']}.corrected.visionproj"
                    else:
                        target = self._artifact_path(project_id, artifact_id, ".zip")
                        if project["format"] == "folder":
                            self._zip_tree(corrected or self._source_path(project), target)
                        else:
                            primary_rel = Path(*PurePosixPath(project["primary_rel"]).parts)
                            extras = {}
                            mapping = self.projects_root / project_id / "path-mapping.json"
                            if mapping.is_file():
                                extras["SAIGE_PATH_MAPPING.json"] = mapping.read_bytes()
                            self._zip_tree(
                                source_root,
                                target,
                                replacement=(primary_rel, corrected) if corrected else None,
                                extras=extras,
                            )
                        filename = f"{project['name']}.corrected.zip"
            except Exception:
                if target is not None:
                    target.unlink(missing_ok=True)
                raise
            finally:
                if work is not None and work.is_dir():
                    self._remove_tree(work)
            assert target is not None
            size = target.stat().st_size
            digest = _stream_sha256(target)
            now = _now()
            connection.execute(
                "INSERT INTO export_artifacts VALUES(?,?,?,?,?,?,?,?)",
                (artifact_id, project_id, actor, filename, str(target), size, digest, now),
            )
            connection.execute(
                "UPDATE projects SET updated_at=?,last_activity=? WHERE id=?",
                (now, now, project_id),
            )
            self._audit(connection, actor, "export.create", project_id, project["revision"], details={"artifact_id": artifact_id, "session_only": session_only})
        return {
            "id": artifact_id,
            "filename": filename,
            "size": size,
            "sha256": digest,
            "download_url": f"/api/projects/{project_id}/exports/{artifact_id}/download",
            "output": filename,
        }

    def artifact(self, actor: str, project_id: str, artifact_id: str) -> tuple[Path, str, str]:
        del actor
        artifact_id = _safe_id(artifact_id, "artifact id")
        with self._connect() as connection:
            project = self._project_row(connection, project_id)
            if project["state"] != "active":
                raise RuntimeError("项目当前不可下载")
            row = connection.execute(
                "SELECT * FROM export_artifacts WHERE id=? AND project_id=?",
                (artifact_id, project_id),
            ).fetchone()
        if row is None:
            raise LookupError("导出文件不存在")
        path = Path(row["path"])
        if not path.is_file() or _stream_sha256(path) != row["sha256"]:
            raise RuntimeError("导出文件完整性校验失败")
        return path, row["filename"], mimetypes.guess_type(row["filename"])[0] or "application/octet-stream"

    def recycle_project(self, actor: str, project_id: str) -> None:
        if not self.is_admin(actor):
            raise PermissionError("仅管理员可删除项目")
        source = self.projects_root / project_id
        target = None
        moved = False
        try:
            with self._lock, self._connect() as connection:
                project = self._project_row(connection, project_id)
                if project["state"] != "active":
                    raise RuntimeError("项目不在可删除状态")
                busy = connection.execute(
                    "SELECT 1 FROM analysis_runs WHERE project_id=? AND state IN ('queued','running')",
                    (project_id,),
                ).fetchone()
                if busy:
                    raise RuntimeError("运行中的项目不能删除")
                target = self.recycle_root / f"{project_id}-{int(_now())}"
                os.replace(source, target)
                moved = True
                now = _now()
                connection.execute(
                    "UPDATE projects SET state='recycled',recycled_at=?,recycle_path=?,updated_at=? WHERE id=?",
                    (now, str(target), now, project_id),
                )
                connection.execute("DELETE FROM leases WHERE project_id=?", (project_id,))
                self._audit(connection, actor, "project.recycle", project_id, project["revision"])
            self._sessions.pop(project_id, None)
        except Exception:
            if moved and target is not None and target.exists() and not source.exists():
                os.replace(target, source)
            raise

    def restore_project(self, actor: str, project_id: str) -> None:
        if not self.is_admin(actor):
            raise PermissionError("仅管理员可恢复项目")
        source = None
        target = self.projects_root / project_id
        moved = False
        try:
            with self._lock, self._connect() as connection:
                project = self._project_row(connection, project_id)
                if project["state"] != "recycled" or not project["recycle_path"]:
                    raise RuntimeError("项目不在回收区")
                source = Path(project["recycle_path"])
                if not source.is_dir() or target.exists():
                    raise RuntimeError("回收数据不存在或目标路径冲突")
                os.replace(source, target)
                moved = True
                now = _now()
                connection.execute(
                    "UPDATE projects SET state='active',recycled_at=NULL,recycle_path=NULL,"
                    "last_activity=?,updated_at=? WHERE id=?",
                    (now, now, project_id),
                )
                self._audit(connection, actor, "project.restore", project_id, project["revision"])
        except Exception:
            if moved and source is not None and target.exists() and not source.exists():
                os.replace(target, source)
            raise

    @staticmethod
    def _remove_tree(path: Path) -> None:
        def onerror(function, value, _exc):
            try:
                os.chmod(value, 0o700)
                function(value)
            except OSError:
                raise

        shutil.rmtree(path, onerror=onerror)

    def cleanup_expired(self) -> None:
        now = _now()
        with self._lock:
            with self._connect() as connection:
                active = connection.execute(
                    "SELECT * FROM projects WHERE state='active' AND last_activity<?",
                    (now - self.config.retention_seconds,),
                ).fetchall()
            for candidate in active:
                source = self.projects_root / candidate["id"]
                target = self.recycle_root / f"{candidate['id']}-{int(now)}"
                moved = False
                try:
                    with self._connect() as connection:
                        project = self._project_row(connection, candidate["id"])
                        if project["state"] != "active" or project["last_activity"] >= now - self.config.retention_seconds:
                            continue
                        busy = connection.execute(
                            "SELECT 1 FROM analysis_runs WHERE project_id=? AND state IN ('queued','running')",
                            (project["id"],),
                        ).fetchone()
                        lease = connection.execute(
                            "SELECT 1 FROM leases WHERE project_id=? AND expires_at>?",
                            (project["id"], now),
                        ).fetchone()
                        if busy or lease or not source.is_dir():
                            continue
                        os.replace(source, target)
                        moved = True
                        connection.execute(
                            "UPDATE projects SET state='recycled',recycled_at=?,recycle_path=?,updated_at=? WHERE id=?",
                            (now, str(target), now, project["id"]),
                        )
                        connection.execute("DELETE FROM leases WHERE project_id=?", (project["id"],))
                        self._audit(connection, "system", "project.expire", project["id"], project["revision"])
                    self._sessions.pop(candidate["id"], None)
                except Exception:
                    if moved and target.exists() and not source.exists():
                        os.replace(target, source)
                    raise

            with self._connect() as connection:
                recycled = connection.execute(
                    "SELECT * FROM projects WHERE state='purging' OR "
                    "(state='recycled' AND recycled_at<?)",
                    (now - self.config.recycle_seconds,),
                ).fetchall()
            for project in recycled:
                if project["state"] == "recycled":
                    with self._connect() as connection:
                        connection.execute(
                            "UPDATE projects SET state='purging',updated_at=? WHERE id=? AND state='recycled'",
                            (now, project["id"]),
                        )
                target = Path(project["recycle_path"] or "")
                if target.exists():
                    if target.parent.resolve() != self.recycle_root.resolve() or not target.is_dir():
                        raise RuntimeError("回收区路径完整性校验失败，已停止永久清理")
                    self._remove_tree(target)
                exports = self.exports_root / project["id"]
                if exports.is_dir():
                    self._remove_tree(exports)
                with self._connect() as connection:
                    connection.execute("DELETE FROM projects WHERE id=? AND state='purging'", (project["id"],))
                    self._audit(connection, "system", "project.purge", project["id"], project["revision"])

            with self._connect() as connection:
                stale_uploads = connection.execute(
                    "SELECT id FROM uploads WHERE updated_at<?", (now - 24 * 60 * 60,)
                ).fetchall()
            for upload in stale_uploads:
                target = self.staging_root / upload["id"]
                if target.is_dir():
                    self._remove_tree(target)
                with self._connect() as connection:
                    connection.execute("DELETE FROM uploads WHERE id=?", (upload["id"],))

    def _cleanup_loop(self) -> None:
        while not self._closed.wait(300):
            try:
                self.cleanup_expired()
            except Exception as error:
                print(f"remote cleanup failed: {type(error).__name__}: {error}")

    def health(self, *, ready: bool = False) -> dict:
        result = {"status": "ok", "version": __version__, "api_version": API_VERSION}
        if ready:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            usage = shutil.disk_usage(self.root)
            dependencies = dependency_status()
            model = model_status(AnalysisConfig(device="auto", model_access="local_only"))
            result.update(
                {
                    "database": "ok",
                    "storage": "ok" if usage.free >= self.config.min_free_space else "low",
                    "analysis_worker": "ok" if self._worker and self._worker.is_alive() else "stopped",
                    "dependencies": "ok" if dependencies.get("ready") else "missing",
                    "model": "ok" if model.get("ready") and model.get("cached") else "missing",
                }
            )
            if any(
                result[key] != "ok"
                for key in ("storage", "analysis_worker", "dependencies", "model")
            ):
                result["status"] = "degraded"
        return result

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._worker:
            self._queue.put(None)
            self._worker.join(timeout=10)
        if self._cleaner:
            self._cleaner.join(timeout=10)
