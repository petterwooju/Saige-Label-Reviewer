from __future__ import annotations

import argparse
import errno
import hashlib
import json
import mimetypes
import os
import secrets
import socket
import sys
import threading
import uuid
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as _ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .adapters import load_source, render_preview
from .analysis import (AnalysisConfig, analyze, dependency_status, model_status,
                       preview_content_digest)
from .demo import make_demo, make_empty
from .exporter import export_corrected
from .session import ReviewSession

STATIC = Path(__file__).parent / "static"
WORKSPACE = Path.cwd() / "workspace"
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8"}


def _safe_print(value: object) -> None:
    """Write a diagnostic without ever changing an application outcome.

    Windows services and GitHub-hosted runners can expose a legacy console
    encoding.  Encoding a translated diagnostic must not turn a successful
    transaction into an HTTP error or terminate a worker thread.
    """
    message = str(value)
    stream = sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        safe_message = message.encode(encoding, errors="backslashreplace").decode(encoding)
        stream.write(safe_message + "\n")
        stream.flush()
    except Exception:
        # Diagnostics are best-effort. The original application result is
        # always more important than a console sink that is closed or broken.
        return


class ThreadingHTTPServer(_ThreadingHTTPServer):
    """HTTP server that never shares a listening port with another process."""

    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class PathPickerError(Exception):
    """Raised when the native file picker cannot provide a usable path."""


_PATH_PICKER_LOCK = threading.Lock()


def _normalize_picked_path(value, kind: str) -> str:
    """Return a canonical existing path without changing application state."""
    if kind not in {"file", "folder"}:
        raise ValueError("kind 必须是 file 或 folder")
    if value is None or value == "":
        return ""
    if not isinstance(value, (str, os.PathLike)):
        raise PathPickerError("文件选择器返回了无效路径")
    try:
        selected = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise PathPickerError("所选路径已不存在或无法访问") from error
    if kind == "file" and not selected.is_file():
        raise PathPickerError("请选择一个文件")
    if kind == "folder" and not selected.is_dir():
        raise PathPickerError("请选择一个文件夹")
    return str(selected)


def pick_local_path(kind: str, purpose: str = "source") -> str:
    """Open the platform-native Tk file dialog and return an absolute path."""
    if kind not in {"file", "folder"}:
        raise ValueError("kind 必须是 file 或 folder")
    if purpose not in {"source", "preview_root"}:
        raise ValueError("purpose 必须是 source 或 preview_root")
    if purpose == "preview_root" and kind != "folder":
        raise ValueError("外部图像目录只能选择文件夹")
    if not _PATH_PICKER_LOCK.acquire(blocking=False):
        raise PathPickerError("已有一个文件选择窗口，请先完成或取消当前选择")

    root = None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            # On Windows this keeps the Explorer dialog above the browser that
            # initiated the request.  Some Tk backends do not support it.
            root.attributes("-topmost", True)
            root.update_idletasks()
        except (AttributeError, tk.TclError):
            pass

        if kind == "file":
            selected = filedialog.askopenfilename(
                parent=root,
                title="选择本地数据源文件",
                filetypes=(
                    ("支持的数据源", "*.json *.srproj *.visionproj"),
                    ("所有文件", "*.*"),
                ),
            )
        else:
            selected = filedialog.askdirectory(
                parent=root,
                title=("选择要授权的外部图像目录" if purpose == "preview_root"
                       else "选择本地数据集文件夹"),
                mustexist=True,
            )
    except Exception as error:
        raise PathPickerError("无法打开本地资源管理器，请确认当前桌面会话可用") from error
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        _PATH_PICKER_LOCK.release()
    return _normalize_picked_path(selected, kind)


@dataclass(slots=True)
class Job:
    id: str | None = None
    state: str = "idle"
    progress: float = 0.0
    message: str = ""
    error: str | None = None
    result: dict | None = None


class AppState:
    def __init__(self, source: Path | None = None, workspace: Path = WORKSPACE,
                 allowed_preview_roots: list[str | Path] | None = None,
                 start_empty: bool = False):
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        dataset = load_source(source) if source else (make_empty() if start_empty else make_demo())
        roots = self._validate_preview_roots(allowed_preview_roots)
        if roots:
            dataset.metadata["allowed_preview_roots"] = roots
        self.session = ReviewSession(dataset)
        self.job = Job()
        self.analysis_config = AnalysisConfig()
        self._session_generation = 0
        self._analysis_thread: threading.Thread | None = None
        self._analysis_stale_notices: dict[str, str] = {}
        self.requires_source_reload = False
        self._restore_session()

    @staticmethod
    def _validate_preview_roots(values) -> list[str]:
        if values is None:
            return []
        if not isinstance(values, (list, tuple)) or len(values) > 8:
            raise ValueError("额外图像根目录必须是最多 8 个路径的数组")
        roots = []
        for value in values:
            if not isinstance(value, (str, Path)) or not str(value).strip():
                raise ValueError("额外图像根目录格式无效")
            candidate = Path(str(value).strip().strip('"')).expanduser()
            if not candidate.is_absolute():
                raise ValueError(f"额外图像根目录必须是绝对路径：{candidate}")
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ValueError(f"额外图像根目录不存在：{candidate}") from error
            if not resolved.is_dir():
                raise ValueError(f"额外图像根目录不是文件夹：{resolved}")
            text = str(resolved)
            if text not in roots:
                roots.append(text)
        return roots

    def _state_path(self) -> Path:
        source = self.session.dataset.source
        if source is None:
            return self.workspace / "sessions" / "demo-v2.json"
        source_identity = os.path.normcase(str(source.resolve()))
        fingerprint = self.session.dataset.source_hash or "missing-source-hash"
        identity = hashlib.sha256(f"{source_identity}\0{fingerprint}".encode("utf-8")).hexdigest()
        return self.workspace / "sessions" / f"v2-{identity}.json"

    def _legacy_state_path(self) -> Path | None:
        if self.session.dataset.source is None or not self.session.dataset.source_hash:
            return None
        return self.workspace / "sessions" / f"{self.session.dataset.source_hash}.json"

    def _restore_session(self) -> None:
        target = self._state_path()
        if target.exists() and self.session.restore(target):
            return
        legacy = self._legacy_state_path()
        if legacy is None or not self.session.restore(legacy):
            return
        # v2 contents include exact source/hash/layout identity, so a legacy
        # hash-only filename is safe to import only after ``restore`` accepts it.
        # Keep the shared legacy file: another source with identical bytes may
        # still need to examine (and reject) it independently.
        try:
            self.session.save(target)
        except OSError as error:
            print(f"无法迁移旧会话文件：{error}")

    def save_session(self) -> None:
        if self.session.dataset.source:
            self.session.save(self._state_path())

    def ensure_idle(self, *, allow_source_reload: bool = False) -> None:
        if self.job.state == "running":
            raise RuntimeError("分析正在运行，请等待完成后再修改、切换或导出项目")
        if self.requires_source_reload and not allow_source_reload:
            raise RuntimeError("源项目已覆盖但自动重载失败；请重新打开数据源后再继续")

    def job_payload(self) -> dict:
        with self.lock:
            return {"job": asdict(self.job)}

    def _recent_path(self) -> Path:
        return self.workspace / "recent.json"

    def recent(self) -> list[dict]:
        try:
            value = json.loads(self._recent_path().read_text(encoding="utf-8"))
            if not isinstance(value, list):
                return []
            entries = []
            for entry in value:
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                    continue
                if Path(entry["path"]).exists():
                    entries.append(entry)
            return entries[:8]
        except (OSError, json.JSONDecodeError, TypeError, RecursionError):
            return []

    def _remember(self, source: Path, allowed_preview_roots: list[str]) -> None:
        entries = [entry for entry in self.recent() if entry.get("path") != str(source)]
        entries.insert(0, {"path": str(source), "name": self.session.dataset.name,
                           "source_type": self.session.dataset.source_type,
                           "allowed_preview_roots": allowed_preview_roots})
        target = self._recent_path()
        temporary = target.with_name(f"{target.name}.tmp-{uuid.uuid4().hex}")
        try:
            temporary.write_text(json.dumps(entries[:8], ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def open(self, source_value: str, allowed_preview_roots=None) -> None:
        with self.lock:
            self.ensure_idle(allow_source_reload=True)
            generation = self._session_generation
        roots = self._validate_preview_roots(allowed_preview_roots)
        source = Path(source_value.strip().strip('"')).expanduser().resolve()
        if not source.exists():
            raise ValueError(f"路径不存在：{source}")
        dataset = load_source(source)
        if roots:
            dataset.metadata["allowed_preview_roots"] = roots
        if not dataset.items:
            raise ValueError("项目中没有可复查的已标注样本")
        with self.lock:
            self.ensure_idle(allow_source_reload=True)
            if generation != self._session_generation:
                raise RuntimeError("数据源已被另一个请求切换，请重试")
            self.session = ReviewSession(dataset)
            self._session_generation += 1
            self.job = Job()
            self._analysis_stale_notices = {}
            self.requires_source_reload = False
            self._restore_session()
            try:
                self._remember(source, roots)
            except OSError as error:
                print(f"无法更新最近项目列表：{error}")

    def authorize_preview_root(self, root_value: str | Path) -> None:
        """Authorize one additional image root without replacing the review session."""
        with self.lock:
            self.ensure_idle()
            source = self.session.dataset.source
            if source is None:
                raise ValueError("请先打开真实数据源")
            validated = self._validate_preview_roots([root_value])[0]
            roots = list(self.session.dataset.metadata.get("allowed_preview_roots") or [])
            normalized = os.path.normcase(validated)
            if not any(os.path.normcase(root) == normalized for root in roots):
                if len(roots) >= 8:
                    raise ValueError("额外图像根目录最多允许 8 个路径")
                roots.append(validated)
            roots = self._validate_preview_roots(roots)
            # Persist first so a failed recent-list write cannot leave a transient,
            # apparently successful authorization in the active session.
            self._remember(source, roots)
            self.session.dataset.metadata["allowed_preview_roots"] = roots

    def payload(self, include_token: str | None = None) -> dict:
        with self.lock:
            value = {**self.session.payload(), "app_version": __version__,
                     "recent": self.recent(),
                     "dependencies": dependency_status(), "job": asdict(self.job),
                     "analysis_config": asdict(self.analysis_config),
                     "model_status": model_status(self.analysis_config),
                     "requires_source_reload": self.requires_source_reload}
            if include_token:
                value["token"] = include_token
            return value

    def start_analysis(self, config_value: dict) -> None:
        config = AnalysisConfig.from_dict(config_value)
        with self.lock:
            self.ensure_idle()
            if not self.session.dataset.source:
                raise ValueError("请先打开真实数据源")
            self.analysis_config = config
            job_id = uuid.uuid4().hex
            self.job = Job(id=job_id, state="running", progress=0.0, message="正在准备分析")
            session = self.session
            generation = self._session_generation

        def current_job() -> bool:
            return (self.session is session and self._session_generation == generation and
                    self.job.id == job_id and self.job.state == "running")

        def progress(value: float, message: str) -> None:
            with self.lock:
                if not current_job():
                    return
                self.job.progress = max(0.0, min(float(value), 1.0))
                self.job.message = message

        def worker() -> None:
            try:
                result = analyze(session.dataset, config, self.workspace / "cache", progress)
                if not isinstance(result, dict):
                    raise ValueError("分析结果格式无效")
                updates = result.get("item_updates")
                public_result = {key: value for key, value in result.items() if key != "item_updates"}
                with self.lock:
                    if not current_job():
                        return
                    session.stage_analysis_updates(updates)
                    self.job = Job(id=job_id, state="completed", progress=1.0,
                                   message="分析完成", result=public_result)
            except Exception as error:
                with self.lock:
                    if current_job():
                        self.job = Job(id=job_id, state="failed", progress=self.job.progress,
                                       message="分析失败", error=str(error))

        thread = None
        try:
            thread = threading.Thread(target=worker, name=f"saige-analysis-{job_id[:8]}", daemon=True)
            with self.lock:
                self._analysis_thread = thread
            thread.start()
        except Exception as error:
            with self.lock:
                if current_job():
                    self.job = Job(id=job_id, state="failed", progress=0.0,
                                   message="分析启动失败", error=str(error))
                if thread is None or self._analysis_thread is thread:
                    self._analysis_thread = None
            raise


def create_handler(state: AppState, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"SaigeReviewer/{__version__}"

        def log_message(self, fmt, *args):
            print(f"[{self.log_date_time_string()}] {fmt % args}")

        def _send(self, body: bytes, content_type: str, status=HTTPStatus.OK):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                "script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, value, status=HTTPStatus.OK):
            self._send(json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

        def _local(self):
            return (self.headers.get("Host") or "").split(":", 1)[0].lower() in {"127.0.0.1", "localhost"}

        def do_GET(self):
            if not self._local():
                return self._json({"error": "仅允许本机访问"}, HTTPStatus.FORBIDDEN)
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/bootstrap":
                return self._json({
                    "api_version": 2,
                    "mode": "local",
                    "app_version": __version__,
                    "capabilities": {
                        "local_path_picker": True,
                        "source_overwrite": True,
                        "shared_projects": False,
                        "resumable_upload": False,
                        "edit_leases": False,
                        "download_exports": False,
                    },
                })
            if path == "/api/session":
                return self._json(state.payload(include_token=token))
            if path == "/api/job":
                return self._json(state.job_payload())
            if path.startswith("/api/preview/"):
                item_id = path.rsplit("/", 1)[-1]
                from urllib.parse import parse_qs
                query = parse_qs(parsed.query)
                diagnose = (query.get("diagnose") or [""])[0] == "1"
                with state.lock:
                    if diagnose:
                        stale_notice = state._analysis_stale_notices.pop(item_id, None)
                        if stale_notice:
                            return self._json(
                                {"analysis_stale": True, "error": stale_notice},
                                HTTPStatus.CONFLICT,
                            )
                    dataset = state.session.dataset
                    item = state.session._item(item_id)
                    expected_digest = (
                        item.metadata.get("_analysis_preview_sha256") if item else None
                    )
                    if expected_digest:
                        try:
                            current_digest = preview_content_digest(dataset, item)
                        except PermissionError as error:
                            return self._json({"error": str(error)}, HTTPStatus.FORBIDDEN)
                        except (OSError, RuntimeError, ValueError):
                            current_digest = None
                        if (current_digest is None or
                                not secrets.compare_digest(expected_digest, current_digest)):
                            state.session.clear_analysis_results()
                            state.job = Job(
                                state="idle",
                                message="分析输入已变化，请重新分析",
                            )
                            stale_notice = (
                                "预览图像已在分析后变化；旧分析结果已清空，"
                                "请重新运行分析。"
                            )
                            state._analysis_stale_notices[item_id] = stale_notice
                            return self._json(
                                {
                                    "analysis_stale": True,
                                    "error": stale_notice,
                                },
                                HTTPStatus.CONFLICT,
                            )
                mode = (query.get("view") or ["crop"])[0]
                show_contours = (query.get("contours") or ["1"])[0] != "0"
                try:
                    preview = render_preview(
                        dataset, item, mode, show_contours=show_contours
                    ) if item else None
                except PermissionError as error:
                    return self._json({"error": str(error)}, HTTPStatus.FORBIDDEN)
                except (OSError, ValueError) as error:
                    return self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                if preview:
                    return self._send(*preview)
                return self._send(b"", "image/png", HTTPStatus.NO_CONTENT)
            name = "index.html" if path == "/" else path.lstrip("/")
            target = (STATIC / name).resolve()
            if STATIC.resolve() not in target.parents or not target.is_file():
                return self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            content_type = STATIC_TYPES.get(target.suffix.lower(), mimetypes.guess_type(target.name)[0] or
                                            "application/octet-stream")
            self._send(target.read_bytes(), content_type)

        def do_POST(self):
            if not self._local() or not secrets.compare_digest(self.headers.get("X-Review-Token") or "", token):
                return self._json({"error": "Forbidden"}, HTTPStatus.FORBIDDEN)
            try:
                length = int(self.headers.get("Content-Length") or "0")
                if length < 0 or length > 256 * 1024:
                    raise ValueError("请求过大")
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("请求正文必须是 JSON 对象")
                path = urlparse(self.path).path
                if path == "/api/pick-path":
                    kind = body.get("kind")
                    if kind not in {"file", "folder"}:
                        raise ValueError("kind 必须是 file 或 folder")
                    purpose = body.get("purpose", "source")
                    if purpose not in {"source", "preview_root"}:
                        raise ValueError("purpose 必须是 source 或 preview_root")
                    if purpose == "preview_root" and kind != "folder":
                        raise ValueError("外部图像目录只能选择文件夹")
                    selected = _normalize_picked_path(pick_local_path(kind, purpose), kind)
                    return self._json({"path": selected, "cancelled": not bool(selected)})
                elif path == "/api/open":
                    state.open(str(body["source"]), body.get("allowed_preview_roots"))
                    return self._json(state.payload())
                elif path == "/api/authorize-preview-root":
                    state.authorize_preview_root(body.get("path"))
                    return self._json(state.payload())
                elif path == "/api/update":
                    with state.lock:
                        state.ensure_idle()
                        item_id = str(body["id"])
                        mutation = state.session.stage_update(item_id, label=body.get("label"),
                                                              status=body.get("status"))
                        if mutation:
                            try:
                                state.save_session()
                            except Exception:
                                state.session.rollback_review(mutation)
                                raise
                        response = state.session.mutation_payload(item_id)
                    return self._json(response)
                elif path == "/api/undo":
                    with state.lock:
                        state.ensure_idle()
                        mutation = state.session.stage_undo()
                        item_id = mutation.command.item_id if mutation else None
                        if mutation:
                            try:
                                state.save_session()
                            except Exception:
                                state.session.rollback_review(mutation)
                                raise
                        response = state.session.mutation_payload(item_id)
                    return self._json(response)
                elif path == "/api/redo":
                    with state.lock:
                        state.ensure_idle()
                        mutation = state.session.stage_redo()
                        item_id = mutation.command.item_id if mutation else None
                        if mutation:
                            try:
                                state.save_session()
                            except Exception:
                                state.session.rollback_review(mutation)
                                raise
                        response = state.session.mutation_payload(item_id)
                    return self._json(response)
                elif path == "/api/analyze":
                    state.start_analysis(body)
                    return self._json(state.job_payload())
                elif path == "/api/export":
                    with state.lock:
                        state.ensure_idle()
                        result = export_corrected(state.session, state.workspace, overwrite=False)
                    return self._json({"export_result": result})
                elif path == "/api/overwrite":
                    if body.get("confirmation") != "OVERWRITE_SOURCE":
                        raise ValueError("覆盖确认无效")
                    with state.lock:
                        state.ensure_idle()
                        roots = list(state.session.dataset.metadata.get("allowed_preview_roots") or [])
                        result = export_corrected(state.session, state.workspace, overwrite=True)
                        source = state.session.dataset.source
                        state.requires_source_reload = True
                        reload_error = None
                        try:
                            state.open(str(source), roots)
                        except Exception as error:
                            # ``open`` may have installed a replacement session and cleared this
                            # flag before restore/recent handling failed.  Once the source has
                            # already been overwritten, no mutation is safe until a complete
                            # reload succeeds.
                            state.requires_source_reload = True
                            reload_error = str(error)
                            _safe_print(
                                f"源项目已覆盖，但自动重载失败：{type(error).__name__}: {error}"
                            )
                    response = {"export_result": result, "reload_required": True}
                    if reload_error:
                        response["reload_error"] = reload_error
                    return self._json(response)
                elif path == "/api/export-session":
                    with state.lock:
                        state.ensure_idle()
                        target_dir = state.workspace / "exports"
                        target_dir.mkdir(parents=True, exist_ok=True)
                        target = target_dir / f"review-session-{uuid.uuid4().hex}.json"
                        try:
                            with target.open("x", encoding="utf-8") as output:
                                json.dump(state.session.export_payload(), output, ensure_ascii=False, indent=2)
                        except Exception:
                            target.unlink(missing_ok=True)
                            raise
                    return self._json({"export_result": {"output": str(target.resolve())}})
                else:
                    return self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            except PathPickerError as error:
                self._json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
            except RuntimeError as error:
                self._json({"error": str(error)}, HTTPStatus.CONFLICT)
            except (KeyError, ValueError, OSError, json.JSONDecodeError) as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            except Exception as error:
                print(f"请求处理失败：{type(error).__name__}: {error}")
                self._json({"error": "服务器内部错误"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    return Handler


def _acquire_instance_lock(workspace: Path):
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    lock_path = workspace / ".instance.lock"
    handle = lock_path.open("a+b")
    if lock_path.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as error:
        handle.close()
        raise RuntimeError("另一个 Saige Label Reviewer 实例正在使用此工作区，请先关闭它") from error
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()).encode("ascii"))
    handle.flush()
    handle.seek(0)
    return handle


def _release_instance_lock(handle) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def run(source: Path | None, host: str, port: int, open_browser: bool = False,
        image_roots: list[Path] | None = None):
    instance_lock = _acquire_instance_lock(WORKSPACE)
    server = None
    try:
        state = AppState(
            source,
            allowed_preview_roots=image_roots,
            start_empty=source is None,
        )
        token = secrets.token_urlsafe(24)
        try:
            server = ThreadingHTTPServer((host, port), create_handler(state, token))
        except OSError as error:
            address_in_use = (error.errno in {errno.EADDRINUSE, 10048} or
                              getattr(error, "winerror", None) == 10048)
            if port == 0 or not address_in_use:
                raise
            server = ThreadingHTTPServer((host, 0), create_handler(state, token))
            print(f"端口 {port} 已被占用，已自动改用空闲端口。")
        actual_port = int(server.server_address[1])
        print(f"Saige标记复查工作台：http://{host}:{actual_port}")
        print(f"数据集：{state.session.dataset.name}；样本：{len(state.session.dataset.items)}；源文件保持不变")
        if open_browser:
            import webbrowser
            threading.Timer(0.7, lambda: webbrowser.open(f"http://{host}:{actual_port}/")).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        if server is not None:
            server.server_close()
        _release_instance_lock(instance_lock)


def main():
    parser = argparse.ArgumentParser(description="Saige标记复查工作台")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--image-root", action="append", type=Path, default=[],
                        help="额外授权的图像根目录；可重复使用")
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    run(args.source, args.host, args.port, args.open_browser, args.image_root)
