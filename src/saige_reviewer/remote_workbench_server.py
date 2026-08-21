from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import __version__
from .remote_projects import CHUNK_SIZE, MAX_JSON_BODY, RemoteProjectService, RemoteRuntimeConfig
from .server import STATIC, STATIC_TYPES, ThreadingHTTPServer, _acquire_instance_lock, _release_instance_lock


class RateLimitError(RuntimeError):
    pass


def _normalize_email(value: object, domain: str) -> str:
    if not isinstance(value, str):
        raise PermissionError("Cloudflare Access 身份缺失")
    email = value.strip().lower()
    local, separator, suffix = email.rpartition("@")
    if not separator or not local or suffix != domain.lower():
        raise PermissionError(f"仅允许 @{domain} 账户")
    if len(email) > 254 or any(character.isspace() for character in email):
        raise PermissionError("无效的邮箱身份")
    return email


class CloudflareAccessVerifier:
    def __init__(self, team_domain: str, audience: str):
        issuer = team_domain.strip().rstrip("/")
        if not issuer.startswith("https://"):
            issuer = f"https://{issuer}"
        if not audience.strip():
            raise ValueError("Cloudflare Access AUD 不能为空")
        self.issuer = issuer
        self.audience = audience.strip()
        self._client = None

    def __call__(self, assertion: str) -> dict:
        try:
            import jwt
        except ImportError as error:
            raise RuntimeError("缺少 PyJWT，不能验证 Cloudflare Access JWT") from error
        if self._client is None:
            self._client = jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs")
        key = self._client.get_signing_key_from_jwt(assertion).key
        claims = jwt.decode(
            assertion,
            key,
            algorithms=["RS256"],
            audience=self.audience,
            issuer=self.issuer,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
        if not isinstance(claims, dict) or claims.get("type") != "app":
            raise PermissionError("Cloudflare Access JWT 类型无效")
        return claims


@dataclass(frozen=True, slots=True)
class RemoteServerConfig:
    expected_hostname: str
    allowed_domain: str = "saigeai.com"
    dev_auth_email: str | None = None
    assertion_verifier: object | None = None
    csrf_secret: bytes = b""

    def __post_init__(self):
        host = self.expected_hostname.strip().lower().rstrip(".")
        domain = self.allowed_domain.strip().lower().lstrip("@").rstrip(".")
        if not host or not domain:
            raise ValueError("expected hostname and allowed domain are required")
        object.__setattr__(self, "expected_hostname", host)
        object.__setattr__(self, "allowed_domain", domain)
        if self.dev_auth_email:
            object.__setattr__(self, "dev_auth_email", _normalize_email(self.dev_auth_email, domain))
        if not self.csrf_secret:
            object.__setattr__(self, "csrf_secret", secrets.token_bytes(32))


def _error_details(error: Exception) -> tuple[str, HTTPStatus, bool, str]:
    if isinstance(error, RateLimitError):
        return "rate_limited", HTTPStatus.TOO_MANY_REQUESTS, True, str(error)
    if isinstance(error, PermissionError):
        return "forbidden", HTTPStatus.FORBIDDEN, False, str(error)
    if isinstance(error, LookupError):
        return "not_found", HTTPStatus.NOT_FOUND, False, str(error)
    if isinstance(error, RuntimeError):
        return "conflict", HTTPStatus.CONFLICT, True, str(error)
    if isinstance(error, (ValueError, OSError, json.JSONDecodeError)):
        return "invalid_request", HTTPStatus.BAD_REQUEST, False, str(error)
    print(f"remote request failed: {type(error).__name__}: {error}")
    return "internal_error", HTTPStatus.INTERNAL_SERVER_ERROR, True, "服务器内部错误"


def create_remote_handler(service: RemoteProjectService, config: RemoteServerConfig):
    rate_lock = threading.Lock()
    rate_buckets: dict[tuple[str, str], tuple[float, float]] = {}

    class Handler(BaseHTTPRequestHandler):
        server_version = f"SaigeRemote/{__version__}"
        sys_version = ""

        def log_message(self, fmt, *args):
            print(f"[remote {self.log_date_time_string()}] {fmt % args}")

        def _security_headers(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'; object-src 'none'",
            )

        def _send(self, body: bytes, content_type: str, status=HTTPStatus.OK, headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, value, status=HTTPStatus.OK, headers=None):
            self._send(
                json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"),
                "application/json; charset=utf-8",
                status,
                headers,
            )

        def _host(self) -> str:
            value = (self.headers.get("Host") or "").strip()
            if value.startswith("["):
                return value.split("]", 1)[0].lstrip("[").lower()
            return value.split(":", 1)[0].lower().rstrip(".")

        def _identity(self) -> str:
            host = self._host()
            if config.dev_auth_email and host in {"127.0.0.1", "localhost", "::1"}:
                return config.dev_auth_email
            if host != config.expected_hostname:
                raise PermissionError("非预期的公网主机名")
            assertion = self.headers.get("Cf-Access-Jwt-Assertion") or ""
            if not 20 <= len(assertion) <= 16_384:
                raise PermissionError("Cloudflare Access assertion 缺失")
            if config.assertion_verifier is None:
                raise PermissionError("源站未配置 Access JWT 验证器")
            try:
                claims = config.assertion_verifier(assertion)
            except PermissionError:
                raise
            except Exception as error:
                raise PermissionError("Cloudflare Access JWT 验证失败") from error
            email = _normalize_email(claims.get("email"), config.allowed_domain)
            forwarded = self.headers.get("Cf-Access-Authenticated-User-Email")
            if forwarded and not hmac.compare_digest(email, _normalize_email(forwarded, config.allowed_domain)):
                raise PermissionError("Cloudflare Access 身份头不一致")
            return email

        def _csrf_token(self, actor: str) -> str:
            return hmac.new(config.csrf_secret, actor.encode("utf-8"), hashlib.sha256).hexdigest()

        def _rate_limit(self, actor: str) -> None:
            now = time.monotonic()
            key = (actor, str(self.client_address[0]))
            with rate_lock:
                tokens, updated = rate_buckets.get(key, (60.0, now))
                tokens = min(60.0, tokens + max(0.0, now - updated) * 10.0)
                if tokens < 1.0:
                    rate_buckets[key] = (tokens, now)
                    raise RateLimitError("请求过于频繁，请稍后重试")
                rate_buckets[key] = (tokens - 1.0, now)
                if len(rate_buckets) > 10_000:
                    stale = [item for item, value in rate_buckets.items() if now - value[1] > 600]
                    for item in stale:
                        rate_buckets.pop(item, None)

        def _require_csrf(self, actor: str):
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie") or "")
            morsel = cookie.get("Saige-CSRF")
            expected = self._csrf_token(actor)
            header = self.headers.get("X-CSRF-Token") or ""
            value = morsel.value if morsel else ""
            if not hmac.compare_digest(header, expected) or not hmac.compare_digest(value, expected):
                raise PermissionError("CSRF 校验失败，请刷新页面")
            origin = self.headers.get("Origin")
            if origin:
                expected_origin = (
                    f"https://{config.expected_hostname}"
                    if self._host() == config.expected_hostname
                    else f"http://{self.headers.get('Host')}"
                )
                if origin.rstrip("/").lower() != expected_origin.rstrip("/").lower():
                    raise PermissionError("请求来源无效")

        def _read_json(self):
            try:
                length = int(self.headers.get("Content-Length") or "-1")
            except ValueError as error:
                raise ValueError("Content-Length 无效") from error
            if not 0 <= length <= MAX_JSON_BODY:
                raise ValueError("JSON 请求正文过大")
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError("JSON 请求正文必须是对象")
            return value

        def _project_id(self) -> str:
            value = self.headers.get("X-Project-ID") or ""
            if not re.fullmatch(r"[0-9a-f]{32}", value):
                raise ValueError("缺少有效的项目 ID")
            return value

        def _lease(self):
            token = self.headers.get("X-Edit-Lease") or ""
            revision = (self.headers.get("If-Match") or "").strip('W/"')
            try:
                return token, int(revision)
            except ValueError as error:
                raise ValueError("缺少有效的 If-Match 项目版本") from error

        def _route_error(self, error):
            code, status, retryable, message = _error_details(error)
            self._json({"error": {"code": code, "message": message, "retryable": retryable}}, status)

        def _send_file(self, path: Path, filename: str, content_type: str):
            size = path.stat().st_size
            start, end, status = 0, size - 1, HTTPStatus.OK
            value = self.headers.get("Range") or ""
            if value:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
                if not match or not any(match.groups()):
                    raise ValueError("仅支持单一字节范围下载")
                left, right = match.groups()
                if left:
                    start, end = int(left), int(right) if right else size - 1
                else:
                    start, end = max(0, size - int(right)), size - 1
                if start < 0 or end < start or start >= size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self._security_headers()
                    self.end_headers()
                    return
                end, status = min(end, size - 1), HTTPStatus.PARTIAL_CONTENT
            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "download.bin"
            self.send_header("Content-Disposition", f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}")
            self._security_headers()
            self.end_headers()
            if self.command == "HEAD":
                return
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        raise OSError("导出文件读取中断")
                    try:
                        self.wfile.write(block)
                    except ConnectionError:
                        # A browser may cancel a duplicate or superseded download after
                        # response headers were sent. The artifact remains intact; do not
                        # attempt to write a JSON error onto the closed response stream.
                        return
                    remaining -= len(block)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            try:
                parsed, path = urlparse(self.path), urlparse(self.path).path
                if path == "/healthz":
                    return self._json(service.health())
                actor = self._identity()
                if path == "/readyz":
                    return self._json(service.health(ready=True))
                if path == "/api/bootstrap":
                    token = self._csrf_token(actor)
                    cookie = f"Saige-CSRF={token}; Path=/; SameSite=Strict"
                    if self._host() == config.expected_hostname:
                        cookie += "; Secure"
                    value = service.bootstrap(actor)
                    value["csrf_token"] = token
                    return self._json(value, headers={"Set-Cookie": cookie})
                if path == "/api/projects":
                    return self._json({"projects": service.list_projects(actor)})
                if path == "/api/admin/recycle":
                    return self._json({"projects": service.list_projects(actor, recycled=True)})
                if path == "/api/admin/audit":
                    query = parse_qs(parsed.query)
                    return self._json(
                        service.audit_events(actor, limit=(query.get("limit") or [200])[0])
                    )
                match = re.fullmatch(r"/api/uploads/([0-9a-f]{32})", path)
                if match:
                    return self._json({"upload": service.get_upload(actor, match.group(1))})
                match = re.fullmatch(r"/api/projects/([0-9a-f]{32})", path)
                if match:
                    return self._json({"project": service.get_project(actor, match.group(1))})
                match = re.fullmatch(r"/api/projects/([0-9a-f]{32})/session", path)
                if match:
                    return self._json(service.session_payload(actor, match.group(1), compact=True))
                match = re.fullmatch(r"/api/projects/([0-9a-f]{32})/items", path)
                if match:
                    query = parse_qs(parsed.query)
                    return self._json(
                        service.list_items(
                            actor,
                            match.group(1),
                            offset=(query.get("offset") or [0])[0],
                            limit=(query.get("limit") or [400])[0],
                            search=(query.get("search") or [""])[0],
                            label=(query.get("label") or ["all"])[0],
                            status=(query.get("status") or ["all"])[0],
                            min_score=(query.get("min_score") or [0])[0],
                        )
                    )
                match = re.fullmatch(r"/api/projects/([0-9a-f]{32})/feature-sample", path)
                if match:
                    query = parse_qs(parsed.query)
                    return self._json(
                        service.feature_sample(
                            actor,
                            match.group(1),
                            limit=(query.get("limit") or [20_000])[0],
                            search=(query.get("search") or [""])[0],
                            label=(query.get("label") or ["all"])[0],
                            status=(query.get("status") or ["all"])[0],
                            min_score=(query.get("min_score") or [0])[0],
                        )
                    )
                match = re.fullmatch(r"/api/projects/([0-9a-f]{32})/references", path)
                if match:
                    query = parse_qs(parsed.query)
                    return self._json(
                        service.references(
                            actor,
                            match.group(1),
                            (query.get("label") or [""])[0],
                            exclude=(query.get("exclude") or [""])[0],
                            limit=(query.get("limit") or [3])[0],
                        )
                    )
                if path == "/api/session":
                    return self._json(service.session_payload(actor, self._project_id(), compact=True))
                match = re.fullmatch(r"/api/projects/([0-9a-f]{32})/job", path)
                if match:
                    return self._json(service.job_payload(actor, match.group(1)))
                if path == "/api/job":
                    return self._json(service.job_payload(actor, self._project_id()))
                match = re.fullmatch(r"/api/projects/([0-9a-f]{32})/preview/([^/]+)", path)
                if match:
                    query = parse_qs(parsed.query)
                    mode = (query.get("view") or ["crop"])[0]
                    if mode not in {"crop", "original"}:
                        raise ValueError("预览模式无效")
                    preview = service.preview(actor, match.group(1), unquote(match.group(2)), mode, (query.get("contours") or ["1"])[0] != "0")
                    return self._send(*preview) if preview else self._send(b"", "image/png", HTTPStatus.NO_CONTENT)
                match = re.fullmatch(r"/api/projects/([0-9a-f]{32})/exports/([0-9a-f]{32})/download", path)
                if match:
                    artifact, filename, content_type = service.artifact(actor, match.group(1), match.group(2))
                    return self._send_file(artifact, filename, content_type)
                name = "index.html" if path == "/" else path.lstrip("/")
                target = (STATIC / name).resolve()
                if STATIC.resolve() not in target.parents or not target.is_file():
                    return self._json({"error": {"code": "not_found", "message": "接口不存在", "retryable": False}}, HTTPStatus.NOT_FOUND)
                content_type = STATIC_TYPES.get(target.suffix.lower(), mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                return self._send(target.read_bytes(), content_type)
            except Exception as error:
                return self._route_error(error)

        def do_POST(self):
            try:
                actor = self._identity()
                self._require_csrf(actor)
                self._rate_limit(actor)
                path, body = urlparse(self.path).path, self._read_json()
                if path == "/api/uploads":
                    return self._json(service.create_upload(actor, body), HTTPStatus.CREATED)
                match = re.fullmatch(r"/api/uploads/([0-9a-f]{32})/files", path)
                if match:
                    return self._json(service.register_files(actor, match.group(1), body.get("files")))
                match = re.fullmatch(r"/api/uploads/([0-9a-f]{32})/complete", path)
                if match:
                    return self._json(service.complete_upload(actor, match.group(1)))
                match = re.fullmatch(r"/api/projects/([0-9a-f]{32})/lock", path)
                if match:
                    return self._json({"lock": service.acquire_lease(actor, match.group(1), force=bool(body.get("force")))})
                match = re.fullmatch(r"/api/projects/([0-9a-f]{32})/lock/heartbeat", path)
                if match:
                    return self._json({"lock": service.heartbeat_lease(actor, match.group(1), self.headers.get("X-Edit-Lease") or "")})
                match = re.fullmatch(r"/api/admin/projects/([0-9a-f]{32})/restore", path)
                if match:
                    service.restore_project(actor, match.group(1))
                    return self._json({"restored": True})
                project_id = self._project_id()
                if path == "/api/export":
                    return self._json({"export_result": service.create_export(actor, project_id)})
                if path == "/api/export-session":
                    return self._json({"export_result": service.create_export(actor, project_id, session_only=True)})
                token, revision = self._lease()
                if path == "/api/update":
                    return self._json(service.mutate(actor, project_id, token, revision, "update", body))
                if path == "/api/undo":
                    return self._json(service.mutate(actor, project_id, token, revision, "undo"))
                if path == "/api/redo":
                    return self._json(service.mutate(actor, project_id, token, revision, "redo"))
                if path == "/api/analyze":
                    return self._json({"job": service.submit_analysis(actor, project_id, token, revision, body)}, HTTPStatus.ACCEPTED)
                return self._json({"error": {"code": "not_found", "message": "接口不存在", "retryable": False}}, HTTPStatus.NOT_FOUND)
            except Exception as error:
                return self._route_error(error)

        def do_PUT(self):
            try:
                actor = self._identity()
                self._require_csrf(actor)
                self._rate_limit(actor)
                match = re.fullmatch(r"/api/uploads/([0-9a-f]{32})/files/([0-9a-f]{32})/chunks/(\d+)", urlparse(self.path).path)
                if not match:
                    return self._json({"error": {"code": "not_found", "message": "接口不存在", "retryable": False}}, HTTPStatus.NOT_FOUND)
                length = int(self.headers.get("Content-Length") or "-1")
                if not 0 <= length <= CHUNK_SIZE:
                    raise ValueError("上传分块过大")
                payload = self.rfile.read(length)
                if len(payload) != length:
                    raise ValueError("上传分块不完整")
                value = service.write_chunk(actor, match.group(1), match.group(2), match.group(3), payload, self.headers.get("X-Chunk-SHA256") or "")
                return self._json({"upload": value})
            except Exception as error:
                return self._route_error(error)

        def do_DELETE(self):
            try:
                actor = self._identity()
                self._require_csrf(actor)
                self._rate_limit(actor)
                parsed, path = urlparse(self.path), urlparse(self.path).path
                match = re.fullmatch(r"/api/projects/([0-9a-f]{32})/lock", path)
                if match:
                    force = (parse_qs(parsed.query).get("force") or ["0"])[0] == "1"
                    service.release_lease(actor, match.group(1), self.headers.get("X-Edit-Lease") or "", force=force)
                    return self._json({"released": True})
                match = re.fullmatch(
                    r"/api/projects/([0-9a-f]{32})/analysis-runs/([0-9a-f]{32})",
                    path,
                )
                if match:
                    token, revision = self._lease()
                    return self._json(
                        {
                            "job": service.cancel_analysis(
                                actor, match.group(1), token, revision, match.group(2)
                            )
                        }
                    )
                match = re.fullmatch(r"/api/admin/projects/([0-9a-f]{32})", path)
                if match:
                    service.recycle_project(actor, match.group(1))
                    return self._json({"recycled": True})
                return self._json({"error": {"code": "not_found", "message": "接口不存在", "retryable": False}}, HTTPStatus.NOT_FOUND)
            except Exception as error:
                return self._route_error(error)

    return Handler


def _settings(root: Path) -> dict:
    path = root / "config" / "remote-config.json"
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def run_remote(*, port: int, expected_hostname: str, allowed_domain: str, workspace: Path,
               dev_auth_email: str | None = None, access_team_domain: str | None = None,
               access_audience: str | None = None) -> None:
    workspace = workspace.resolve()
    runtime, values = RemoteRuntimeConfig.load(workspace), _settings(workspace)
    team = access_team_domain or values.get("access_team_domain")
    audience = access_audience or values.get("access_audience")
    verifier = CloudflareAccessVerifier(str(team), str(audience)) if team and audience else None
    if not dev_auth_email and verifier is None:
        raise RuntimeError("生产远程服务必须配置 access_team_domain 和 access_audience")
    instance_lock = _acquire_instance_lock(workspace)
    service, server = RemoteProjectService(runtime), None
    try:
        config = RemoteServerConfig(expected_hostname, allowed_domain, dev_auth_email, verifier)
        server = ThreadingHTTPServer(("127.0.0.1", int(port)), create_remote_handler(service, config))
        print(f"Saige remote workbench origin: http://127.0.0.1:{server.server_address[1]}")
        print(f"Expected public hostname: {config.expected_hostname}")
        print(f"Allowed email domain: @{config.allowed_domain}")
        print("The loopback origin requires a verified Cloudflare Access JWT.")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.server_close()
        service.close()
        _release_instance_lock(instance_lock)


def main() -> None:
    default_root = Path(r"E:\remote\SaigeLabelReviewer") if os.name == "nt" else Path.cwd() / "workspace" / "remote"
    parser = argparse.ArgumentParser(description="Saige Label Reviewer remote workbench")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--expected-hostname", default="saige-label-reviewer-beta.saigeai.com")
    parser.add_argument("--allowed-domain", default="saigeai.com")
    parser.add_argument("--workspace", type=Path, default=default_root)
    parser.add_argument("--access-team-domain")
    parser.add_argument("--access-aud", dest="access_audience")
    parser.add_argument("--dev-auth-email", help="仅用于 loopback 开发；Tunnel 服务中禁止使用")
    args = parser.parse_args()
    run_remote(port=args.port, expected_hostname=args.expected_hostname,
               allowed_domain=args.allowed_domain, workspace=args.workspace,
               dev_auth_email=args.dev_auth_email, access_team_domain=args.access_team_domain,
               access_audience=args.access_audience)


if __name__ == "__main__":
    main()
