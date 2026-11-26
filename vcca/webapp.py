"""vCCA web front end — a pure standard-library WSGI application.

Local:   ``vcca_plan.py serve`` (wsgiref + threads, 127.0.0.1:8770)
Server:  ``gunicorn --workers 1 --threads 8 vcca.webapp:application`` (see deploy/DEPLOY.md). One process only:
         the job state, sessions cache and report cache live in this process (jobs/sessions are also on disk).

Accounts: ``STATE_DIR/users.toml`` (PBKDF2-SHA256 + salt), managed with ``vcca_plan.py web-user set <name>``.
Sessions: random token in an ``HttpOnly; SameSite=Strict`` cookie (``Secure`` over HTTPS), 12 h; every write request
must carry ``X-VCCA-CSRF`` = the session's CSRF token (returned by /api/login and /api/me). Failed logins are
rate-limited per client address.

Routes (all JSON unless noted; everything except /, /static/*, /api/login needs a session):
  GET  /  /static/<file>                         app shell (vcca/web)
  POST /api/login  /api/logout   GET /api/me
  GET  /api/seasons            POST /api/seasons {name, copy_from}
  GET  /api/status?season=     (vcca.status.collect_status)
  GET  /api/runs?season=       GET /api/runs/<run>?season=     GET /api/runs/<run>/report?season=&prev=auto|none|<run>
  GET  /api/runs/<run>/zip?season=                          (zip download)
  GET  /files/<season>/<run>/<file>                         (download one file of a run folder)
  GET  /api/jobs   POST /api/jobs {cmd, season, opts}   GET /api/jobs/<id>?since=<byte>   POST /api/jobs/<id>/cancel
  GET  /api/config?season=     GET /api/config/history?season=&key=&version=
  POST /api/config/validate|save|restore {season, key, text|version, mtime_ns}
  GET  /api/data?season=       POST /api/upload/vamsys (raw body)    POST /api/upload/plan?season=&add=1 (raw body)
"""

from __future__ import annotations

import base64
import datetime as _dt
import gzip
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import tomllib
import traceback
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable

from . import config as C
from .jobs import JobBusy, JobManager, RUN_RE, SEASON_RE

WEB_DIR = Path(__file__).resolve().parent / "web"
USERS_FILE = "users.toml"
SESSIONS_FILE = "sessions.json"
COOKIE = "vcca_session"
CSRF_HEADER = "HTTP_X_VCCA_CSRF"
SESSION_TTL_S = 12 * 3600
USERNAME_RE = re.compile(r"[A-Za-z0-9_.\-]{1,32}")
PBKDF2_ITERATIONS = 310_000
MAX_JSON = 4 * 1024 * 1024
VAMSYS_BACKUPS_KEPT = 5
PLAN_NAME_RE = re.compile(r"[\w㐀-鿿（）()\-.+ ]{1,120}\.xls", re.IGNORECASE)
CONFIG_KEYS = {"season": "season.toml（本航季）", "airport_grades": "rules/airport_grades.toml",
               "fleets": "rules/fleets.toml"}
SQLITE_MAGIC = b"SQLite format 3\x00"
OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
SEASON_TEMPLATE = """# vCCA 航季配置（网页新建 {created}）。每个键都有默认值，见 fr24_work/vcca/config.py DEFAULTS。
[season]
name = "{name}"
# start_date = "YYYY-MM-DD"   # 留空：取计划 FD23/FD24 众数
# end_date   = "YYYY-MM-DD"
plan_files = [
{files},
]

[vamsys]
db_path      = "Data/workspaces.db"   # 相对数据目录 (VCCA_PLAN_ROOT)
workspace_id = ""                     # 必填：VAMSYS 工作区 UUID
collection   = 1

[export]
default_cost_index = {{ CCA = 80, CAO = 150, CCD = 80, BJN = 80 }}
"""
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def max_upload_bytes() -> int:
    try:
        return int(float(os.environ.get("VCCA_MAX_UPLOAD_MB", "512")) * 1024 * 1024)
    except ValueError:
        return 512 * 1024 * 1024


# =====================================================================================================================
# accounts
# =====================================================================================================================
def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"pbkdf2_sha256${iterations}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, it, salt, dk = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(it))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def load_users(state_dir: Path) -> dict[str, dict]:
    p = Path(state_dir) / USERS_FILE
    try:
        d = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    users = d.get("users", {})
    return {k: v for k, v in users.items() if USERNAME_RE.fullmatch(k) and isinstance(v, dict) and v.get("hash")}


def _write_users(state_dir: Path, users: dict[str, dict]) -> None:
    lines = ["# vCCA 网页前端账号（PBKDF2-SHA256 哈希，不存明文）。用 vcca_plan.py web-user set <名字> 管理。", ""]
    for name in sorted(users):
        u = users[name]
        lines.append(f'[users."{name}"]')
        for k in ("hash", "created", "updated"):
            if u.get(k):
                lines.append(f'{k} = "{u[k]}"')
        lines.append("")
    p = Path(state_dir) / USERS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp, p)


def set_user(state_dir: Path, name: str, password: str, *, iterations: int = PBKDF2_ITERATIONS) -> bool:
    """Create the account or change its password. Returns True when created."""
    if not USERNAME_RE.fullmatch(name):
        raise ValueError("invalid user name")
    users = load_users(state_dir)
    created = name not in users
    u = users.get(name, {"created": _utcnow_iso()})
    u["hash"] = hash_password(password, iterations=iterations)
    u["updated"] = _utcnow_iso()
    users[name] = u
    _write_users(state_dir, users)
    return created


def delete_user(state_dir: Path, name: str) -> bool:
    users = load_users(state_dir)
    if name not in users:
        return False
    del users[name]
    _write_users(state_dir, users)
    return True


# =====================================================================================================================
# HTTP plumbing
# =====================================================================================================================
class HTTPError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


STATUS_TEXT = {200: "OK", 204: "No Content", 304: "Not Modified", 400: "Bad Request", 401: "Unauthorized",
               403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
               411: "Length Required", 413: "Payload Too Large", 415: "Unsupported Media Type",
               422: "Unprocessable Entity", 429: "Too Many Requests", 500: "Internal Server Error"}


class Request:
    def __init__(self, environ: dict):
        self.environ = environ
        self.method = environ.get("REQUEST_METHOD", "GET").upper()
        raw = environ.get("PATH_INFO", "") or "/"
        try:
            self.path = raw.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            self.path = raw
        self.query = {k: v[-1] for k, v in urllib.parse.parse_qs(environ.get("QUERY_STRING", ""),
                                                                   keep_blank_values=True).items()}
        self.session: dict | None = None
        self.user: str | None = None

    def header(self, name: str, default: str = "") -> str:
        return self.environ.get("HTTP_" + name.upper().replace("-", "_"), default)

    @property
    def content_length(self) -> int | None:
        try:
            return int(self.environ.get("CONTENT_LENGTH") or "")
        except ValueError:
            return None

    def json(self) -> dict:
        n = self.content_length or 0
        if n > MAX_JSON:
            raise HTTPError(413, "请求过大")
        body = self.environ["wsgi.input"].read(n) if n else b""
        try:
            d = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            raise HTTPError(400, "请求不是合法 JSON") from None
        if not isinstance(d, dict):
            raise HTTPError(400, "请求应为 JSON 对象")
        return d

    def cookies(self) -> dict[str, str]:
        out = {}
        for part in self.environ.get("HTTP_COOKIE", "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def client_ip(self) -> str:
        if os.environ.get("VCCA_TRUST_PROXY") == "1":
            xff = self.environ.get("HTTP_X_FORWARDED_FOR", "")
            if xff:
                return xff.split(",")[0].strip()
        return self.environ.get("REMOTE_ADDR", "?")

    def is_https(self) -> bool:
        if self.environ.get("wsgi.url_scheme") == "https":
            return True
        return os.environ.get("VCCA_TRUST_PROXY") == "1" and \
            self.environ.get("HTTP_X_FORWARDED_PROTO", "").lower() == "https"

    def accepts_gzip(self) -> bool:
        return "gzip" in self.environ.get("HTTP_ACCEPT_ENCODING", "")


class Response:
    def __init__(self, body: bytes | Iterable[bytes] = b"", status: int = 200, content_type: str = "application/json",
                 headers: list[tuple[str, str]] | None = None, length: int | None = None):
        self.body, self.status, self.content_type = body, status, content_type
        self.headers = list(headers or [])
        self.length = length


def json_response(obj: Any, status: int = 200, headers: list[tuple[str, str]] | None = None) -> Response:
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    return Response(body, status, "application/json; charset=utf-8", headers)


def content_disposition(name: str) -> str:
    """attachment with an ASCII fallback and the RFC 5987 UTF-8 name (Chinese file names)."""
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(name, safe='')}"


def _file_iter(path: Path, chunk: int = 256 * 1024, cleanup: Callable[[], None] | None = None) -> Iterable[bytes]:
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                yield b
    finally:
        if cleanup:
            cleanup()


def _safe_child(base: Path, name: str) -> Path:
    """``base/name`` when ``name`` is a plain file name of an existing regular file directly inside ``base``."""
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name or name.startswith("."):
        raise HTTPError(404, "文件不存在")
    if name not in os.listdir(base):
        raise HTTPError(404, "文件不存在")
    p = base / name
    if p.is_symlink() or not p.is_file():
        raise HTTPError(404, "文件不存在")
    return p


def _file_meta(p: Path) -> dict[str, Any]:
    if not p.is_file():
        return {"name": p.name, "path": str(p), "exists": False}
    st = p.stat()
    return {"name": p.name, "path": str(p), "exists": True, "size": st.st_size, "mtime": st.st_mtime}


def _kind(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {"csv": "csv", "xlsx": "xlsx", "html": "html", "json": "json", "log": "log"}.get(ext, "other")


def csv_import_order(names: Iterable[str]) -> list[str]:
    """FlightOpsDesk import order: 01 → 02 → 02b → 03 → 04, review (待复核) files last."""
    return sorted(names, key=lambda n: ("待复核" in n, n))


def _toml_error(e: tomllib.TOMLDecodeError) -> dict[str, Any]:
    msg = str(e)
    m = re.search(r"\(at line (\d+), column (\d+)\)", msg)
    return {"message": msg, "line": int(m.group(1)) if m else None, "col": int(m.group(2)) if m else None}


# =====================================================================================================================
# the application
# =====================================================================================================================
class App:
    def __init__(self, state_dir: Path | None = None, *, job_manager: JobManager | None = None):
        self.state_dir = Path(state_dir or C.state_dir())
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "tmp").mkdir(exist_ok=True)
        self.jobs = job_manager or JobManager(self.state_dir)
        self._lock = threading.RLock()
        self._sessions: dict[str, dict] = self._load_sessions()
        self._fails: dict[str, list[float]] = {}            # ip -> [count, locked_until]
        self._report_cache: dict[tuple, bytes] = {}         # key -> gzipped JSON
        self._status_cache: dict[str, tuple[tuple, float, dict]] = {}
        self._vamsys_cache: tuple[tuple, Any] | None = None
        self._write_lock = threading.RLock()                # config saves / uploads, one at a time (re-entrant:
                                                            # a plan upload may also update season.toml)
        self.routes: list[tuple[str, re.Pattern, Callable, bool]] = [
            ("GET", re.compile(r"/"), self.h_index, False),
            ("GET", re.compile(r"/static/(?P<name>[A-Za-z0-9_.\-]+)"), self.h_static, False),
            ("POST", re.compile(r"/api/login"), self.h_login, False),
            ("POST", re.compile(r"/api/logout"), self.h_logout, True),
            ("GET", re.compile(r"/api/me"), self.h_me, True),
            ("GET", re.compile(r"/api/seasons"), self.h_seasons, True),
            ("POST", re.compile(r"/api/seasons"), self.h_season_create, True),
            ("GET", re.compile(r"/api/status"), self.h_status, True),
            ("GET", re.compile(r"/api/runs"), self.h_runs, True),
            ("GET", re.compile(r"/api/runs/(?P<run>[^/]+)"), self.h_run, True),
            ("GET", re.compile(r"/api/runs/(?P<run>[^/]+)/report"), self.h_report, True),
            ("GET", re.compile(r"/api/runs/(?P<run>[^/]+)/zip"), self.h_zip, True),
            ("GET", re.compile(r"/files/(?P<season>[^/]+)/(?P<run>[^/]+)/(?P<name>[^/]+)"), self.h_file, True),
            ("GET", re.compile(r"/api/jobs"), self.h_jobs, True),
            ("POST", re.compile(r"/api/jobs"), self.h_job_start, True),
            ("GET", re.compile(r"/api/jobs/(?P<jid>[^/]+)"), self.h_job, True),
            ("POST", re.compile(r"/api/jobs/(?P<jid>[^/]+)/cancel"), self.h_job_cancel, True),
            ("GET", re.compile(r"/api/config"), self.h_config, True),
            ("GET", re.compile(r"/api/config/history"), self.h_config_history, True),
            ("POST", re.compile(r"/api/config/validate"), self.h_config_validate, True),
            ("POST", re.compile(r"/api/config/save"), self.h_config_save, True),
            ("POST", re.compile(r"/api/config/restore"), self.h_config_restore, True),
            ("GET", re.compile(r"/api/data"), self.h_data, True),
            ("POST", re.compile(r"/api/upload/vamsys"), self.h_upload_vamsys, True),
            ("POST", re.compile(r"/api/upload/plan"), self.h_upload_plan, True),
        ]

    # ------------------------------------------------------------------ WSGI entry
    def __call__(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        req = Request(environ)
        try:
            resp = self.dispatch(req)
        except HTTPError as e:
            resp = json_response({"error": e.message, **e.extra}, e.status)
        except Exception as e:  # noqa: BLE001 - never leak a traceback page; log it to stderr
            traceback.print_exc()
            resp = json_response({"error": f"服务器内部错误: {type(e).__name__}: {e}"}, 500)
        return self._finish(req, resp, start_response)

    def _finish(self, req: Request, resp: Response, start_response: Callable) -> Iterable[bytes]:
        headers = [("Content-Type", resp.content_type)] + resp.headers
        body = resp.body
        if isinstance(body, (bytes, bytearray)):
            ct = resp.content_type
            if (req.accepts_gzip() and len(body) > 1024 and not any(h[0] == "Content-Encoding" for h in headers)
                    and (ct.startswith(("application/json", "text/")) or "javascript" in ct)):
                body = gzip.compress(bytes(body), 5)
                headers += [("Content-Encoding", "gzip"), ("Vary", "Accept-Encoding")]
            headers.append(("Content-Length", str(len(body))))
            body = [bytes(body)]
        elif resp.length is not None:
            headers.append(("Content-Length", str(resp.length)))
        names = {h[0].lower() for h in headers}
        if "cache-control" not in names:
            headers.append(("Cache-Control", "no-store"))
        headers += [("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "same-origin"),
                    ("X-Frame-Options", "DENY"), ("Content-Security-Policy", CSP)]
        start_response(f"{resp.status} {STATUS_TEXT.get(resp.status, 'OK')}", headers)
        if req.method == "HEAD":
            return [b""]
        return body

    def dispatch(self, req: Request) -> Response:
        method = "GET" if req.method == "HEAD" else req.method
        allowed = []
        for m, rx, handler, auth in self.routes:
            mt = rx.fullmatch(req.path)
            if not mt:
                continue
            if m != method:
                allowed.append(m)
                continue
            if auth:
                self._authenticate(req)
                if method != "GET":
                    self._check_csrf(req)
            return handler(req, **mt.groupdict())
        if allowed:
            raise HTTPError(405, "方法不允许")
        raise HTTPError(404, "没有这个地址")

    def shutdown(self) -> None:
        self.jobs.shutdown()

    # ------------------------------------------------------------------ sessions
    def _load_sessions(self) -> dict[str, dict]:
        try:
            d = json.loads((self.state_dir / SESSIONS_FILE).read_text(encoding="utf-8"))
            now = time.time()
            return {k: v for k, v in d.items() if v.get("expires", 0) > now}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_sessions(self) -> None:
        p = self.state_dir / SESSIONS_FILE
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(self._sessions), encoding="utf-8")
        os.replace(tmp, p)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass

    @staticmethod
    def _token_key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _authenticate(self, req: Request) -> None:
        token = req.cookies().get(COOKIE, "")
        if token:
            with self._lock:
                s = self._sessions.get(self._token_key(token))
                if s and s["expires"] > time.time() and s["user"] in load_users(self.state_dir):
                    req.session, req.user = s, s["user"]
                    return
        raise HTTPError(401, "请先登录")

    def _check_csrf(self, req: Request) -> None:
        sent = req.environ.get(CSRF_HEADER, "")
        if not sent or not req.session or not hmac.compare_digest(sent, req.session["csrf"]):
            raise HTTPError(403, "CSRF 校验失败，请刷新页面")

    def _cookie(self, req: Request, value: str, max_age: int) -> tuple[str, str]:
        parts = [f"{COOKIE}={value}", "Path=/", "HttpOnly", "SameSite=Strict", f"Max-Age={max_age}"]
        if req.is_https():
            parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))

    def h_login(self, req: Request) -> Response:
        ip = req.client_ip()
        now = time.time()
        with self._lock:
            cnt, until = self._fails.get(ip, [0, 0.0])
        if until > now:
            raise HTTPError(429, f"登录失败次数过多，请 {int(until - now) + 1} 秒后再试", retry_after=int(until - now) + 1)
        d = req.json()
        name, pw = str(d.get("username", "")).strip(), str(d.get("password", ""))
        users = load_users(self.state_dir)
        ok = name in users and verify_password(pw, users[name]["hash"])
        if not ok:
            if name not in users:   # same work as a real check, so timing does not reveal user names
                verify_password(pw, hash_password("x", iterations=1000))
            with self._lock:
                cnt += 1
                delay = 0 if cnt < 3 else min(2 ** (cnt - 3), 300)
                self._fails[ip] = [cnt, now + delay]
            raise HTTPError(401, "用户名或密码错误" + (f"（{delay} 秒后可再试）" if delay else ""))
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self._lock:
            self._fails.pop(ip, None)
            self._sessions = {k: v for k, v in self._sessions.items() if v["expires"] > now}
            self._sessions[self._token_key(token)] = {"user": name, "csrf": csrf, "created": now,
                                                      "expires": now + SESSION_TTL_S}
            self._save_sessions()
        return json_response({"user": name, "csrf": csrf}, headers=[self._cookie(req, token, SESSION_TTL_S)])

    def h_logout(self, req: Request) -> Response:
        token = req.cookies().get(COOKIE, "")
        with self._lock:
            self._sessions.pop(self._token_key(token), None)
            self._save_sessions()
        return json_response({"ok": True}, headers=[self._cookie(req, "", 0)])

    def h_me(self, req: Request) -> Response:
        return json_response({"user": req.user, "csrf": req.session["csrf"],
                              "plan_root": str(C.plan_root()), "max_upload_mb": max_upload_bytes() // 2 ** 20})

    # ------------------------------------------------------------------ static
    def h_index(self, req: Request) -> Response:
        return self.h_static(req, "index.html")

    def h_static(self, req: Request, name: str) -> Response:
        p = _safe_child(WEB_DIR, name)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        return Response(p.read_bytes(), 200, ctype, [("Cache-Control", "no-cache")])

    # ------------------------------------------------------------------ seasons / status
    def _season_dir(self, name: str | None, *, required: bool = False) -> Path:
        seasons = C.discover_seasons(C.plan_root())
        if not name:
            if required or not seasons:
                raise HTTPError(404, "没有航季（PLAN_ROOT 下没有含 season.toml 的文件夹）")
            return seasons[-1]
        if not SEASON_RE.fullmatch(name):
            raise HTTPError(400, "航季名不合法")
        for s in seasons:
            if s.name == name:
                return s
        raise HTTPError(404, f"没有航季 {name}")

    def h_seasons(self, req: Request) -> Response:
        out = []
        for p in C.discover_seasons(C.plan_root()):
            item: dict[str, Any] = {"name": p.name}
            try:
                d = tomllib.loads((p / C.SEASON_FILE).read_text(encoding="utf-8")).get("season", {})
                item.update(label=d.get("name") or p.name, start_date=str(d.get("start_date") or ""),
                            end_date=str(d.get("end_date") or ""))
            except (OSError, tomllib.TOMLDecodeError) as e:
                item.update(label=p.name, error=str(e))
            out.append(item)
        return json_response({"seasons": out, "default": out[-1]["name"] if out else None,
                              "plan_root": str(C.plan_root())})

    def h_season_create(self, req: Request) -> Response:
        d = req.json()
        name, src = str(d.get("name", "")).strip(), str(d.get("copy_from", "")).strip()
        if not SEASON_RE.fullmatch(name):
            raise HTTPError(400, "航季文件夹名只能用字母、数字、. _ -（如 2027-1）")
        dst = C.plan_root() / name
        if dst.exists() and (dst / C.SEASON_FILE).exists():
            raise HTTPError(409, f"航季 {name} 已存在")
        if src or C.discover_seasons(C.plan_root()):
            src_dir = self._season_dir(src or None)
            text = f"# 由网页从航季 {src_dir.name} 复制（{_utcnow_iso()}）\n" + \
                (src_dir / C.SEASON_FILE).read_text(encoding="utf-8")
        else:                       # first season on a fresh server: minimal template (workspace_id to fill in)
            text = SEASON_TEMPLATE.format(name=name, created=_utcnow_iso(),
                                          files=",\n".join(f'    "{f}"' for f in C.DEFAULTS["season"]["plan_files"]))
        dst.mkdir(parents=True, exist_ok=True)
        (dst / C.SEASON_FILE).write_text(text, encoding="utf-8")
        return json_response({"ok": True, "season": name,
                              "next": "上传本季 CAAC 计划 .xls，并在配置页修改 [season] 的 name / 日期 / plan_files"})

    def h_status(self, req: Request) -> Response:
        sdir = self._season_dir(req.query.get("season"))
        from .status import collect_status

        sig = self._status_signature(sdir)
        with self._lock:
            hit = self._status_cache.get(sdir.name)
        if hit and hit[0] == sig and time.time() - hit[1] < 60 and req.query.get("refresh") != "1":
            data = hit[2]
        else:
            data = collect_status(sdir)
            with self._lock:
                self._status_cache[sdir.name] = (sig, time.time(), data)
        out = dict(data)
        out["job"] = self.jobs.current()
        out["server_time"] = time.time()
        out["server_tz"] = time.strftime("%z")
        return json_response(out)

    def _status_signature(self, sdir: Path) -> tuple:
        paths = [sdir / C.SEASON_FILE, C.rules_dir() / "airport_grades.toml", C.rules_dir() / "fleets.toml",
                 sdir / "output"]
        paths += list(sdir.glob("*.xls"))
        try:
            cfg = C.load_config(sdir, strict=False, derive_dates=False)
            paths += [cfg.vamsys_db, cfg.cache_db]
        except C.ConfigError:
            pass
        sig = []
        for p in paths:
            try:
                st = p.stat()
                sig.append((str(p), st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append((str(p), None))
        return tuple(sig)

    # ------------------------------------------------------------------ runs
    def _cfg(self, sdir: Path):
        try:
            return C.load_config(sdir, strict=False, derive_dates=False)
        except C.ConfigError as e:
            raise HTTPError(422, "配置错误，先在配置页修正", errors=e.errors) from None

    def _run_dir(self, sdir: Path, run: str) -> Path:
        if not RUN_RE.fullmatch(run or ""):
            raise HTTPError(404, "运行目录名不合法")
        p = sdir / C.OUTPUT_SUBDIR / run
        if not p.is_dir():
            raise HTTPError(404, f"没有运行 {run}")
        return p

    @staticmethod
    def _summary(run_dir: Path) -> dict | None:
        try:
            return json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def h_runs(self, req: Request) -> Response:
        sdir = self._season_dir(req.query.get("season"))
        out_root = sdir / C.OUTPUT_SUBDIR
        runs = []
        if out_root.is_dir():
            for p in sorted((x for x in out_root.iterdir() if x.is_dir() and RUN_RE.fullmatch(x.name)),
                            key=lambda x: x.name, reverse=True):
                names = os.listdir(p)
                s = self._summary(p) or {}
                runs.append({
                    "name": p.name, "has_decisions": "decisions.json" in names, "has_report": "report.html" in names,
                    "exported": "summary.json" in names, "files": len(names),
                    "csv": csv_import_order(n for n in names if n.endswith(".csv")),
                    "counts": s.get("counts"), "csv_counts": s.get("csv"), "needs_review": s.get("needs_review"),
                    "closure_ok": s.get("closure_ok"), "generated_at": s.get("generated_at"),
                    "exported_at": s.get("exported_at"), "warnings": s.get("warnings") or [],
                })
        return json_response({"season": sdir.name, "runs": runs})

    def h_run(self, req: Request, run: str) -> Response:
        sdir = self._season_dir(req.query.get("season"))
        rd = self._run_dir(sdir, run)
        files = []
        for n in os.listdir(rd):
            p = rd / n
            if p.is_file() and not n.startswith("."):
                st = p.stat()
                files.append({"name": n, "size": st.st_size, "mtime": st.st_mtime, "kind": _kind(n)})
        order = {n: i for i, n in enumerate(csv_import_order(f["name"] for f in files if f["kind"] == "csv"))}
        kind_rank = {"csv": 0, "xlsx": 1, "html": 2, "json": 3, "log": 4, "other": 5}
        files.sort(key=lambda f: (kind_rank[f["kind"]], order.get(f["name"], 0), f["name"]))
        log_tail = ""
        try:
            log_tail = "\n".join((rd / "run.log").read_text(encoding="utf-8", errors="replace").splitlines()[-200:])
        except OSError:
            pass
        earlier = sorted((x.name for x in (sdir / C.OUTPUT_SUBDIR).iterdir()
                          if x.is_dir() and RUN_RE.fullmatch(x.name) and x.name < run
                          and (x / "decisions.json").is_file()), reverse=True)
        return json_response({"season": sdir.name, "name": run, "files": files, "summary": self._summary(rd),
                              "log_tail": log_tail, "earlier_runs": earlier})

    def _vamsys_for_labels(self, cfg) -> Any:
        try:
            st = cfg.vamsys_db.stat()
        except OSError:
            return None
        key = (str(cfg.vamsys_db), st.st_mtime_ns, cfg.workspace_id)
        with self._lock:
            if self._vamsys_cache and self._vamsys_cache[0] == key:
                return self._vamsys_cache[1]
        try:
            from . import vamsys

            vs = vamsys.load_vamsys(cfg)
        except Exception:  # noqa: BLE001 - labels are cosmetic; fall back to fleet IDs
            vs = None
        with self._lock:
            self._vamsys_cache = (key, vs)
        return vs

    def h_report(self, req: Request, run: str) -> Response:
        from . import export_html

        sdir = self._season_dir(req.query.get("season"))
        rd = self._run_dir(sdir, run)
        if not (rd / "decisions.json").is_file():
            raise HTTPError(404, "这次运行没有 decisions.json")
        cfg = self._cfg(sdir)
        prev_q = req.query.get("prev", "auto") or "auto"
        if prev_q == "auto":
            pdir = export_html.previous_run(cfg, rd)
        elif prev_q == "none":
            pdir = None
        else:
            pdir = self._run_dir(sdir, prev_q)
            if not (pdir / "decisions.json").is_file():
                raise HTTPError(404, f"运行 {prev_q} 没有 decisions.json")
        vs = self._vamsys_for_labels(cfg)

        def mt(p: Path | None):
            try:
                return (p / "decisions.json").stat().st_mtime_ns if p else None
            except OSError:
                return None
        key = (sdir.name, run, mt(rd), pdir.name if pdir else None, mt(pdir), id(vs))
        with self._lock:
            blob = self._report_cache.get(key)
        if blob is None:
            payload = export_html.payload_for_run(cfg, rd, prev=pdir, vamsys=vs)
            payload["run"] = run
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
            blob = gzip.compress(raw, 5)
            with self._lock:
                if len(self._report_cache) >= 4:
                    self._report_cache.pop(next(iter(self._report_cache)))
                self._report_cache[key] = blob
        if req.accepts_gzip():
            return Response(blob, 200, "application/json; charset=utf-8",
                            [("Content-Encoding", "gzip"), ("Vary", "Accept-Encoding")])
        return Response(gzip.decompress(blob), 200, "application/json; charset=utf-8")

    def h_file(self, req: Request, season: str, run: str, name: str) -> Response:
        sdir = self._season_dir(season, required=True)
        p = _safe_child(self._run_dir(sdir, run), name)
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if p.suffix.lower() in (".csv", ".log", ".json"):
            ctype = {".csv": "text/csv", ".log": "text/plain", ".json": "application/json"}[p.suffix.lower()] \
                + "; charset=utf-8"
        return Response(_file_iter(p), 200, ctype, [("Content-Disposition", content_disposition(p.name))],
                        length=p.stat().st_size)

    def h_zip(self, req: Request, run: str) -> Response:
        sdir = self._season_dir(req.query.get("season"))
        rd = self._run_dir(sdir, run)
        fd, tmp_name = tempfile.mkstemp(suffix=".zip", dir=self.state_dir / "tmp")
        os.close(fd)
        tmp = Path(tmp_name)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for n in sorted(os.listdir(rd)):
                p = rd / n
                if p.is_file() and not p.is_symlink() and not n.startswith("."):
                    z.write(p, f"{sdir.name}_{run}/{n}")

        def cleanup():
            try:
                tmp.unlink()
            except OSError:
                pass
        return Response(_file_iter(tmp, cleanup=cleanup), 200, "application/zip",
                        [("Content-Disposition", content_disposition(f"{sdir.name}_{run}.zip"))],
                        length=tmp.stat().st_size)

    # ------------------------------------------------------------------ jobs
    def h_jobs(self, req: Request) -> Response:
        return json_response({"current": self.jobs.current(), "jobs": self.jobs.list(int(req.query.get("limit", 30)))})

    def h_job_start(self, req: Request) -> Response:
        d = req.json()
        season = d.get("season") or None
        if season:
            self._season_dir(season)
        opts = d.get("opts") or {}
        if not isinstance(opts, dict):
            raise HTTPError(400, "opts 应为对象")
        try:
            job = self.jobs.start(str(d.get("cmd", "")), season, opts, user=req.user or "")
        except JobBusy as e:
            raise HTTPError(409, str(e), job=e.job) from None
        except ValueError as e:
            raise HTTPError(400, str(e)) from None
        if job["cmd"] in ("build", "export", "run", "fetch"):
            with self._lock:
                self._status_cache.clear()
        return json_response({"job": job}, 201)

    def h_job(self, req: Request, jid: str) -> Response:
        try:
            since = max(0, int(req.query.get("since", "0")))
        except ValueError:
            since = 0
        d = self.jobs.read_log(jid, since)
        if d is None:
            raise HTTPError(404, "没有这个任务")
        return json_response(d)

    def h_job_cancel(self, req: Request, jid: str) -> Response:
        job = self.jobs.cancel(jid)
        if job is None:
            raise HTTPError(404, "没有这个任务")
        return json_response({"job": job})

    def _require_idle(self) -> None:
        cur = self.jobs.current()
        if cur:
            raise HTTPError(409, f"任务 {cur['cmd']} 正在运行，完成后再修改配置或数据", job=cur)

    # ------------------------------------------------------------------ config
    def _config_paths(self, sdir: Path, key: str) -> tuple[Path, Path]:
        """(file, history dir)."""
        if key == "season":
            return sdir / C.SEASON_FILE, sdir / ".config_history"
        if key in ("airport_grades", "fleets"):
            return C.rules_dir() / f"{key}.toml", C.rules_dir() / ".history"
        raise HTTPError(400, "未知配置文件")

    @staticmethod
    def _history(hist: Path, stem: str) -> list[dict]:
        if not hist.is_dir():
            return []
        out = []
        for p in hist.glob(f"{stem}.*.toml"):
            m = re.fullmatch(re.escape(stem) + r"\.(\d{8}-\d{6}(?:-\d+)?)\.toml", p.name)
            if m:
                st = p.stat()
                out.append({"version": m.group(1), "size": st.st_size, "mtime": st.st_mtime})
        return sorted(out, key=lambda x: x["version"], reverse=True)

    def h_config(self, req: Request) -> Response:
        sdir = self._season_dir(req.query.get("season"))
        files = []
        for key, label in CONFIG_KEYS.items():
            path, hist = self._config_paths(sdir, key)
            try:
                text = path.read_bytes().decode("utf-8")
                mtime_ns = path.stat().st_mtime_ns
            except OSError:
                text, mtime_ns = "", None
            files.append({"key": key, "label": label, "path": str(path), "text": text, "mtime_ns": mtime_ns,
                          "history": self._history(hist, path.stem)})
        return json_response({"season": sdir.name, "files": files, "busy": self.jobs.current() is not None})

    def h_config_history(self, req: Request) -> Response:
        sdir = self._season_dir(req.query.get("season"))
        path, hist = self._config_paths(sdir, req.query.get("key", ""))
        version = req.query.get("version", "")
        if not re.fullmatch(r"\d{8}-\d{6}(?:-\d+)?", version):
            raise HTTPError(400, "版本号不合法")
        p = hist / f"{path.stem}.{version}.toml"
        if not p.is_file():
            raise HTTPError(404, "没有这个历史版本")
        return json_response({"version": version, "text": p.read_bytes().decode("utf-8", errors="replace")})

    def _validate(self, sdir: Path, path: Path, text: str) -> tuple[int, dict]:
        """Full validation of ``text`` as the content of ``path`` without touching the file."""
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            return 400, {"ok": False, "stage": "syntax", "syntax": _toml_error(e), "errors": [str(e)]}
        try:
            with C.text_overrides({path: text}):
                cfg = C.load_config(sdir, strict=False)
        except C.ConfigError as e:
            return 422, {"ok": False, "stage": "config", "errors": list(e.errors)}
        except Exception as e:  # noqa: BLE001 - e.g. unreadable plan file during date derivation
            return 422, {"ok": False, "stage": "config", "errors": [f"{type(e).__name__}: {e}"]}
        return 200, {"ok": True, "warnings": list(cfg.warnings),
                     "summary": {"season": cfg.season_label, "start": str(cfg.start_date), "end": str(cfg.end_date),
                                 "ops": list(cfg.target_ops), "effective_time": cfg.effective_time()}}

    def h_config_validate(self, req: Request) -> Response:
        d = req.json()
        sdir = self._season_dir(d.get("season"))
        path, _ = self._config_paths(sdir, str(d.get("key", "")))
        status, res = self._validate(sdir, path, str(d.get("text", "")))
        return json_response(res, status)

    def _save_config(self, sdir: Path, key: str, text: str, mtime_ns: Any) -> tuple[int, dict]:
        self._require_idle()
        path, hist = self._config_paths(sdir, key)
        with self._write_lock:
            try:
                cur_mtime = path.stat().st_mtime_ns
            except OSError:
                cur_mtime = None
            if mtime_ns != cur_mtime:
                return 409, {"ok": False, "stage": "conflict",
                             "errors": ["文件在你打开之后被修改过（其他人或其他程序），请重新加载后再改"],
                             "mtime_ns": cur_mtime}
            status, res = self._validate(sdir, path, text)
            if not res["ok"]:
                return status, res
            hist.mkdir(parents=True, exist_ok=True)
            backup = None
            if path.is_file():
                backup = hist / f"{path.stem}.{_stamp()}.toml"
                n = 1
                while backup.exists():
                    backup = hist / f"{path.stem}.{_stamp()}-{n}.toml"
                    n += 1
                shutil.copy2(path, backup)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(text.encode("utf-8"))
            os.replace(tmp, path)
            try:                                    # re-check from disk; restore the old file if it fails
                C.load_config(sdir, strict=False)
            except Exception as e:  # noqa: BLE001
                if backup:
                    shutil.copy2(backup, path)
                return 422, {"ok": False, "stage": "config", "errors": [f"保存后校验失败，已还原: {e}"]}
            with self._lock:
                self._status_cache.clear()
            res.update(saved=True, backup=backup.name if backup else None, mtime_ns=path.stat().st_mtime_ns)
            return 200, res

    def h_config_save(self, req: Request) -> Response:
        d = req.json()
        sdir = self._season_dir(d.get("season"))
        status, res = self._save_config(sdir, str(d.get("key", "")), str(d.get("text", "")), d.get("mtime_ns"))
        return json_response(res, status)

    def h_config_restore(self, req: Request) -> Response:
        d = req.json()
        sdir = self._season_dir(d.get("season"))
        key = str(d.get("key", ""))
        path, hist = self._config_paths(sdir, key)
        version = str(d.get("version", ""))
        p = hist / f"{path.stem}.{version}.toml"
        if not re.fullmatch(r"\d{8}-\d{6}(?:-\d+)?", version) or not p.is_file():
            raise HTTPError(404, "没有这个历史版本")
        status, res = self._save_config(sdir, key, p.read_bytes().decode("utf-8"), d.get("mtime_ns"))
        return json_response(res, status)

    # ------------------------------------------------------------------ data
    def _vamsys_target(self, sdir: Path) -> tuple[Path, Any]:
        try:
            cfg = C.load_config(sdir, strict=False, derive_dates=False)
            return cfg.vamsys_db, cfg
        except C.ConfigError:
            return C.plan_root() / "Data" / "workspaces.db", None

    @staticmethod
    def _vamsys_backups(target: Path) -> list[Path]:
        rx = re.compile(re.escape(target.stem) + r"\.\d{8}-\d{6}\.db")
        return sorted((p for p in target.parent.glob(f"{target.stem}.*.db") if rx.fullmatch(p.name)),
                      key=lambda p: p.name, reverse=True) if target.parent.is_dir() else []

    def h_data(self, req: Request) -> Response:
        sdir = self._season_dir(req.query.get("season"))
        target, cfg = self._vamsys_target(sdir)
        vs = _file_meta(target)
        if vs["exists"] and cfg is not None:
            try:
                from . import vamsys

                vs["revision"], vs["storage_revision"] = vamsys.read_revision(cfg)
            except Exception as e:  # noqa: BLE001
                vs["error"] = str(e)
        vs["workspace_id"] = cfg.workspace_id if cfg else None
        vs["backups"] = [_file_meta(p) for p in self._vamsys_backups(target)]
        configured = []
        try:
            configured = list(tomllib.loads((sdir / C.SEASON_FILE).read_text(encoding="utf-8"))
                              .get("season", {}).get("plan_files", C.DEFAULTS["season"]["plan_files"]))
        except (OSError, tomllib.TOMLDecodeError):
            pass
        names = sorted(set(configured) | {p.name for p in sdir.glob("*.xls")})
        plan = []
        for n in names:
            m = _file_meta(sdir / n)
            m["configured"] = n in configured
            plan.append(m)
        return json_response({"season": sdir.name, "season_dir": str(sdir), "vamsys": vs, "plan_files": plan,
                              "max_upload_mb": max_upload_bytes() // 2 ** 20, "busy": self.jobs.current() is not None})

    def _receive(self, req: Request, dest_dir: Path, magic: bytes) -> Path:
        """Stream the raw request body into a temp file in ``dest_dir`` (same file system as the target)."""
        n = req.content_length
        if n is None:
            raise HTTPError(411, "缺少 Content-Length")
        if n > max_upload_bytes():
            raise HTTPError(413, f"文件超过上限 {max_upload_bytes() // 2 ** 20} MB")
        if n < len(magic):
            raise HTTPError(400, "文件太小")
        dest_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".upload-", suffix=".tmp", dir=dest_dir)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "wb") as f:
                left, first = n, True
                inp = req.environ["wsgi.input"]
                while left > 0:
                    b = inp.read(min(left, 1024 * 1024))
                    if not b:
                        raise HTTPError(400, "上传中断")
                    if first:
                        if not b[:len(magic)] == magic[:len(b)]:
                            raise HTTPError(400, "文件格式不对")
                        first = False
                    f.write(b)
                    left -= len(b)
            with open(tmp, "rb") as f:
                if f.read(len(magic)) != magic:
                    raise HTTPError(400, "文件格式不对")
            return tmp
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def h_upload_vamsys(self, req: Request) -> Response:
        self._require_idle()
        sdir = self._season_dir(req.query.get("season"))
        target, cfg = self._vamsys_target(sdir)
        with self._write_lock:
            wal = target.with_name(target.name + "-wal")
            if wal.is_file() and wal.stat().st_size > 0:
                raise HTTPError(409, "数据库正被其他程序使用（存在未合并的 -wal 文件），请先关闭 VAMSYS 客户端")
            try:
                tmp = self._receive(req, target.parent, SQLITE_MAGIC)
            except HTTPError as e:
                if e.message == "文件格式不对":
                    e.message = "不是 SQLite 数据库文件（workspaces.db）"
                raise
            try:
                info = self._check_vamsys(tmp, cfg)
            except Exception as e:  # noqa: BLE001
                tmp.unlink(missing_ok=True)
                raise HTTPError(400, f"无法读取上传的数据库: {e}") from None
            finally:   # read-only opens of a WAL-mode file leave empty -wal/-shm side files behind
                for side in ("-wal", "-shm"):
                    sp = tmp.with_name(tmp.name + side)
                    try:
                        if side == "-shm" or sp.stat().st_size == 0:
                            sp.unlink()
                    except OSError:
                        pass
            backup = None
            try:
                if target.is_file():
                    backup = target.with_name(f"{target.stem}.{_stamp()}.db")
                    os.replace(target, backup)
                os.replace(tmp, target)
            except OSError as e:
                tmp.unlink(missing_ok=True)
                if backup and backup.is_file() and not target.exists():
                    os.replace(backup, target)
                raise HTTPError(409, f"替换失败（文件可能被占用）: {e}") from None
            for old in self._vamsys_backups(target)[VAMSYS_BACKUPS_KEPT:]:
                old.unlink(missing_ok=True)
            with self._lock:
                self._status_cache.clear()
                self._vamsys_cache = None
        return json_response({"ok": True, "path": str(target), "backup": backup.name if backup else None, **info,
                              "next": "数据已更新，建议运行 build（或 run）重新计算"})

    @staticmethod
    def _check_vamsys(path: Path, cfg: Any) -> dict:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            need = {"workspaces", "workspace_revisions", "resource_rows"}
            if need - tables:
                raise ValueError(f"缺少表 {sorted(need - tables)}")
            ws = [r[0] for r in conn.execute("SELECT id FROM workspaces")]
        finally:
            conn.close()
        info: dict[str, Any] = {"workspaces": ws}
        if cfg is not None:
            from . import vamsys

            if cfg.workspace_id not in ws:
                raise ValueError(f"数据库里没有配置的工作区 {cfg.workspace_id}")
            vs = vamsys.load_vamsys(cfg, db_path=path)
            c = vs.counts()
            info.update(workspace_name=vs.workspace_name, revision=vs.revision, storage_revision=vs.storage_revision,
                        routes=c["routes"], airports=c["airports"])
        return info

    def h_upload_plan(self, req: Request) -> Response:
        self._require_idle()
        sdir = self._season_dir(req.query.get("season"), required=True)
        raw_name = urllib.parse.unquote(req.header("X-Filename"))
        name = Path(raw_name.replace("\\", "/")).name.strip()
        if not PLAN_NAME_RE.fullmatch(name) or name.startswith("."):
            raise HTTPError(400, "文件名不合法（需要 .xls，只能含中英文、数字、空格、括号、- _ .）")
        add = req.query.get("add") == "1"
        with self._write_lock:
            try:
                tmp = self._receive(req, sdir, OLE_MAGIC)
            except HTTPError as e:
                if e.message == "文件格式不对":
                    e.message = "不是 Excel 97-2003 (.xls) 文件"
                raise
            try:
                info = self._check_plan(tmp, sdir)
            except Exception as e:  # noqa: BLE001
                tmp.unlink(missing_ok=True)
                raise HTTPError(400, f"无法解析计划文件: {e}") from None
            dest = sdir / name
            backup = None
            if dest.is_file():
                hist = sdir / ".plan_history"
                hist.mkdir(exist_ok=True)
                backup = hist / f"{dest.stem}.{_stamp()}.xls"
                shutil.copy2(dest, backup)
            os.replace(tmp, dest)
            res: dict[str, Any] = {"ok": True, "name": name, "backup": backup.name if backup else None, **info}
            if add:
                res["config"] = self._add_plan_file(sdir, name)
            with self._lock:
                self._status_cache.clear()
        res["next"] = "计划已更新，建议运行 build（或 run）重新计算"
        return json_response(res)

    def _check_plan(self, path: Path, sdir: Path) -> dict:
        from . import caac

        rows, sheets = 0, set()
        for sname, _, _ in caac.iter_plan_rows(path):
            rows += 1
            sheets.add(sname)
        if not rows:
            raise ValueError("没有数据行")
        info: dict[str, Any] = {"rows": rows, "sheets": sorted(sheets)}
        try:
            cfg = C.load_config(sdir, strict=False, derive_dates=False)
            fd23, fd24 = caac.fd23_fd24_modes([path], cfg.target_ops)
            info.update(fd23=fd23, fd24=fd24)
        except C.ConfigError:
            pass
        return info

    @staticmethod
    def _set_season_array(text: str, key: str, values: list[str], *, multiline: bool = False) -> str | None:
        """Replace (or add) ``key = [...]`` inside the [season] table of season.toml text; comments elsewhere stay.
        None when there is no [season] table."""
        nl = "\r\n" if "\r\n" in text else "\n"          # keep the file's line endings
        if multiline:
            arr = f"{key} = [{nl}" + "".join(f"    {json.dumps(v, ensure_ascii=False)},{nl}" for v in values) + "]"
        else:
            arr = f"{key} = [" + ", ".join(json.dumps(v, ensure_ascii=False) for v in values) + "]"
        ms = re.search(r"(?m)^\[season\][ \t]*(#[^\r\n]*)?(?=\r?$)", text)
        if not ms:
            return None
        nxt = re.search(r"(?m)^\[", text[ms.end():])                  # next table header (column 0)
        end = ms.end() + nxt.start() if nxt else len(text)
        m = re.search(r"(?ms)^" + re.escape(key) + r"\s*=\s*\[.*?\]", text[ms.end():end])
        if m:
            return text[:ms.end() + m.start()] + arr + text[ms.end() + m.end():]
        return text[:ms.end()] + nl + arr + text[ms.end():]

    def _add_plan_file(self, sdir: Path, name: str) -> dict:
        """Append ``name`` to [season] plan_files in season.toml (validated, backed up). Returns a result dict."""
        path = sdir / C.SEASON_FILE
        text = path.read_bytes().decode("utf-8")
        data = tomllib.loads(text)
        files = list(data.get("season", {}).get("plan_files", C.DEFAULTS["season"]["plan_files"]))
        if name in files:
            return {"ok": True, "changed": False}
        files.append(name)
        new = self._set_season_array(text, "plan_files", files, multiline=True)
        groups = list(data.get("season", {}).get("plan_groups") or [])
        if groups and new is not None:      # explicit labels must stay one per file
            new = self._set_season_array(new, "plan_groups", groups + [C._default_group_label(name)])
        if new is None:
            return {"ok": False, "errors": ["season.toml 没有 [season] 节，请在配置页手动添加 plan_files"]}
        status, res = self._save_config(sdir, "season", new, path.stat().st_mtime_ns)
        res["changed"] = res.get("ok", False)
        return res


# =====================================================================================================================
# entry points
# =====================================================================================================================
_DEFAULT: App | None = None
_DEFAULT_LOCK = threading.Lock()


def create_app(state_dir: Path | None = None) -> App:
    return App(state_dir)


def application(environ: dict, start_response: Callable) -> Iterable[bytes]:
    """WSGI entry for gunicorn (``vcca.webapp:application``); the App is created on first request."""
    global _DEFAULT
    if _DEFAULT is None:
        with _DEFAULT_LOCK:
            if _DEFAULT is None:
                _DEFAULT = create_app()
                import atexit

                atexit.register(_DEFAULT.shutdown)      # worker exit: interrupt a running job cleanly
    return _DEFAULT(environ, start_response)


__all__ = ["App", "application", "create_app", "hash_password", "verify_password", "load_users", "set_user",
           "delete_user", "content_disposition", "csv_import_order", "USERS_FILE", "USERNAME_RE"]
