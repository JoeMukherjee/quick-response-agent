"""Local web server for the live dashboard and bench controls (stdlib only, no build step, works offline).

Security model (the page can trigger helpdesk actions, so it is locked down):
  * binds to 127.0.0.1 only;
  * a random token is printed/opened once, then kept in an HttpOnly SameSite=Strict cookie;
  * every request must carry the cookie (or X-QRA-Token) and a Host header for the loopback address (blocks DNS rebinding);
  * every POST needs a custom header (forces a CORS preflight the server never grants) and a same-origin Origin;
  * a real run, a reset and the final run need an explicit typed confirmation; dry-run and eval are the defaults.
The server never raises to the client: handler errors become a JSON 500 and the process keeps serving.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import mimetypes
import re
import secrets
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from qra import __version__
from qra.core.config import Settings
from qra.infra.cache import clear_store, store_stats
from qra.runtime.service import telemetry_path
from qra.runtime.tryout import MAX_BODY_BYTES, CaseCatalog, TryError, parse_ticket, run_try
from qra.server.jobs import JobManager, parse_api
from qra.telemetry.analytics import DECISION_TPM_LIMIT, HELPDESK_RPS_LIMIT, TelemetryReader

log = logging.getLogger("qra.server")
STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "qra_token"


SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
COMPRESSIBLE = ("text/", "application/json", "javascript", "image/svg")
IMMUTABLE = "public, max-age=31536000, immutable"  # hashed build files never change under the same name


@dataclass(frozen=True)
class Asset:
    mtime_ns: int
    raw: bytes
    gz: bytes | None
    etag: str
    ctype: str


_ASSETS: dict[Path, Asset] = {}
_ASSET_LOCK = threading.Lock()


def load_asset(path: Path) -> Asset:
    """Read a static file once and keep it in memory with its gzip form and ETag; reload only if it changes on disk."""
    mtime = path.stat().st_mtime_ns
    with _ASSET_LOCK:
        hit = _ASSETS.get(path)
        if hit and hit.mtime_ns == mtime:
            return hit
        raw = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or "javascript" in ctype:
            ctype += "; charset=utf-8"
        compress = len(raw) > 512 and any(k in ctype for k in COMPRESSIBLE)
        asset = Asset(mtime, raw, gzip.compress(raw, 9, mtime=0) if compress else None,
                      '"' + hashlib.sha1(raw).hexdigest()[:20] + '"', ctype)
        _ASSETS[path] = asset
        return asset


class QraServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, settings: Settings, token: str | None = None, jobs: JobManager | None = None):
        super().__init__(addr, Handler)
        self.settings = settings
        self.token = token or secrets.token_urlsafe(24)
        self.jobs = jobs or JobManager(settings)
        self.cases = CaseCatalog(settings)
        self.reader = TelemetryReader(telemetry_path(settings))
        self.port = self.server_address[1]

    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}


class Handler(BaseHTTPRequestHandler):
    server: QraServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet: telemetry is the log
        log.debug(fmt, *args)

    # ---- plumbing ------------------------------------------------------------------------------
    def _accepts_gzip(self) -> bool:
        return "gzip" in self.headers.get("Accept-Encoding", "")

    def _send(self, code: int, body: bytes, ctype: str = "application/json", headers: dict | None = None,
              cache: str = "no-store", compress: bool = False) -> None:
        headers = dict(headers or {})
        if compress and len(body) >= 1024 and self._accepts_gzip():
            body = gzip.compress(body, 5, mtime=0)  # a fast level: live JSON is compressed on every request
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if code != HTTPStatus.NOT_MODIFIED:
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8"), compress=True)

    def _cookie_token(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return ""

    def _authorised(self) -> bool:
        if self.headers.get("Host", "") not in self.server.allowed_hosts():
            return False
        supplied = self._cookie_token() or self.headers.get("X-QRA-Token", "")
        return secrets.compare_digest(supplied, self.server.token)

    def _guard(self, post: bool) -> bool:
        if not self._authorised():
            self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return False
        if post:
            origin = self.headers.get("Origin")
            ok_origin = origin is None or origin in {f"http://{h}" for h in self.server.allowed_hosts()}
            if not ok_origin or self.headers.get("X-QRA") != "1":
                self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return False
        return True

    # ---- GET -----------------------------------------------------------------------------------------
    def do_GET(self) -> None:
        try:
            url = urlparse(self.path)
            if url.path == "/":
                return self._index(parse_qs(url.query))
            if url.path.startswith("/static/"):
                return self._static(url.path[len("/static/"):])
            if not url.path.startswith("/api/"):
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            if not self._guard(post=False):
                return
            q = parse_qs(url.query)
            run = (q.get("run") or [None])[0]
            if url.path == "/api/health":
                return self._json(200, {"ok": True, "version": __version__, "job": self.server.jobs.status(),
                                        "limits": {"decision_tpm": DECISION_TPM_LIMIT, "helpdesk_rps": HELPDESK_RPS_LIMIT},
                                        "models": {"judge": self.server.settings.model_judge, "draft": self.server.settings.model_draft},
                                        "ticket_api": self.server.settings.helpdesk_url, "judge_default": self.server.settings.judge_backend})
            if url.path == "/api/runs":
                return self._json(200, {"runs": self.server.reader.runs()})
            if url.path == "/api/snapshot":
                return self._json(200, self.server.reader.snapshot(run))
            if url.path == "/api/job":
                return self._json(200, self.server.jobs.status())
            if url.path == "/api/cases":
                return self._json(200, self.server.cases.get())
            if url.path == "/api/cache":
                return self._json(200, {"namespaces": store_stats(self.server.settings.state_dir / "cache.db")})
            if url.path in ("/api/trace", "/api/faults"):
                key = (q.get("ticket" if url.path == "/api/trace" else "class") or [""])[0]
                if not SAFE_ID.fullmatch(key):
                    return self._json(400, {"error": "a ticket id or failure class is required (letters, digits, - and _)"})
                if url.path == "/api/trace":
                    return self._json(200, {"ticket": key, "events": self.server.reader.trace(key, run)})
                return self._json(200, {"class": key, "events": self.server.reader.faults_of(key)})
            if url.path == "/api/stream":
                return self._stream(run)
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # never let a handler take the server down
            log.exception("GET %s failed", self.path)
            try:
                self._json(500, {"error": f"internal error ({type(exc).__name__})"})
            except Exception:
                pass

    def _try(self, body: dict) -> None:
        """Decide one ticket (stored or typed or pasted) with the chosen model. Read-only: nothing is posted."""
        try:
            raw = body.get("json") if body.get("json") not in (None, "") else body.get("ticket")
            ticket = parse_ticket(raw)
            judge, retrieval = body.get("judge") or self.server.settings.judge_backend, body.get("retrieval", "exhaustive")
            opinion = body.get("second_opinion")
            if opinion is not None and not isinstance(opinion, bool):
                raise TryError("second_opinion must be true or false")
            self._json(200, run_try(self.server.settings, ticket, judge=judge, retrieval=retrieval, second_opinion=opinion))
        except TryError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            log.exception("try failed")
            code, message = _try_error(exc)
            self._json(code, {"error": message})

    def _index(self, query: dict) -> None:
        supplied = (query.get("t") or [""])[0]
        if self.headers.get("Host", "") not in self.server.allowed_hosts():
            return self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
        if supplied and secrets.compare_digest(supplied, self.server.token):
            # first visit with the one-time URL token: set the cookie and drop the token from the address bar
            return self._send(HTTPStatus.FOUND, b"", "text/plain", {
                "Location": "/", "Set-Cookie": f"{COOKIE}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"})
        if not self._authorised():
            return self._send(HTTPStatus.FORBIDDEN, b"Open the URL printed by `python -m qra serve`.", "text/plain")
        self._serve_asset(STATIC / "index.html", "no-cache")  # revalidated with an ETag: a reload is a 304

    def _static(self, name: str) -> None:
        path = (STATIC / name).resolve()
        if STATIC.resolve() not in path.parents or not path.is_file():
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        self._serve_asset(path, IMMUTABLE if name.startswith("assets/") else "no-cache")

    def _serve_asset(self, path: Path, cache: str) -> None:
        asset = load_asset(path)
        base = {"ETag": asset.etag, "Vary": "Accept-Encoding"}
        if self.headers.get("If-None-Match") == asset.etag:
            return self._send(HTTPStatus.NOT_MODIFIED, b"", asset.ctype, base, cache)
        if asset.gz is not None and self._accepts_gzip():
            return self._send(200, asset.gz, asset.ctype, {**base, "Content-Encoding": "gzip"}, cache)
        self._send(200, asset.raw, asset.ctype, base, cache)

    def _stream(self, run: str | None) -> None:
        """Server-sent events: a fresh snapshot every 700 ms until the client goes away."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        last, last_sent = None, 0.0
        try:
            while True:
                snapshot = self.server.reader.snapshot(run)
                job = self.server.jobs.status()
                build = load_asset(STATIC / "index.html").etag.strip('"')  # the page reloads itself when this changes
                # `now` changes on every call; everything else changing is what the page needs to hear about
                digest = hashlib.sha1(json.dumps([{k: v for k, v in snapshot.items() if k != "now"}, job],
                                                 default=str, sort_keys=True).encode("utf-8")).digest()
                if digest != last:
                    body = json.dumps({"snapshot": snapshot, "job": job, "build": build}, ensure_ascii=False, default=str, separators=(",", ":"))
                    self.wfile.write(b"data: " + body.encode("utf-8") + b"\n\n")
                    last, last_sent = digest, time.monotonic()
                elif time.monotonic() - last_sent >= 3.0:  # nothing changed: a tiny heartbeat keeps the page's "live" light honest
                    self.wfile.write(b'data: {"hb":1,"build":"' + build.encode() + b'"}\n\n')
                    last_sent = time.monotonic()
                self.wfile.flush()
                time.sleep(0.7)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # ---- POST ----------------------------------------------------------------------------------------
    def do_POST(self) -> None:
        try:
            # Read the (bounded) body before anything else: answering a request whose body is still unread makes
            # the socket reset on some systems, and the client then sees an abort instead of our 403 or 404.
            route = urlparse(self.path).path
            cap = MAX_BODY_BYTES if route == "/api/try" else 4096  # only a ticket is allowed to be long
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, cap)) if length > 0 else b""
            if length > cap:
                self.close_connection = True  # too large to drain: answer, then drop the connection
            if route not in ("/api/job", "/api/cache", "/api/try"):
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            if not self._guard(post=True):
                return
            if length > cap:
                return self._json(413, {"error": f"request too large (limit {cap} bytes)"})
            body = json.loads(raw or b"{}")
            if route == "/api/try":
                return self._try(body)
            if route == "/api/cache":
                ns = body.get("namespace", "all")
                if ns not in ("all", "decisions", "drafts"):
                    return self._json(400, {"error": "namespace must be all, decisions or drafts"})
                removed = clear_store(self.server.settings.state_dir / "cache.db", None if ns == "all" else ns)
                return self._json(200, {"ok": True, "removed": removed})
            kind = body.get("kind")
            dry = bool(body.get("dry_run", True))
            if kind == "run" and not dry and body.get("confirm") != "RUN":
                return self._json(400, {"error": "a real run needs the typed confirmation RUN"})
            if kind == "reset" and body.get("confirm") != "RESET":
                return self._json(400, {"error": "reset needs the typed confirmation RESET"})
            if kind == "final" and body.get("confirm") != "FINAL":
                return self._json(400, {"error": "the final run re-opens every practice ticket and answers them live: type FINAL to confirm"})
            retrieval = body.get("retrieval", "exhaustive")
            if retrieval not in ("exhaustive", "cascade"):
                return self._json(400, {"error": "retrieval must be exhaustive or cascade"})
            if body.get("suite", "all") not in ("nordavo", "ours", "all"):
                return self._json(400, {"error": "suite must be nordavo, ours or all"})
            judge = body.get("judge") or self.server.settings.judge_backend
            if judge not in ("gemini", "typesafe"):
                return self._json(400, {"error": "judge must be gemini or typesafe"})
            try:
                api = parse_api(self.server.settings, body.get("api"))
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            ok, info = self.server.jobs.start(kind, workers=body.get("workers", 4), dry_run=dry, retrieval=retrieval, judge=judge,
                                              suite=body.get("suite", "all"), api=api)
            return self._json(200 if ok else 409, {"ok": ok, "run_id" if ok else "error": info})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            log.exception("POST %s failed", self.path)
            try:
                self._json(500, {"error": f"internal error ({type(exc).__name__})"})
            except Exception:
                pass


def _try_error(exc: Exception) -> tuple[int, str]:
    """A message for the person: what failed and whether to retry, never a stack trace or a key."""
    name = type(exc).__name__
    if name in ("FatalAuthError", "Unauthorized"):
        return 502, "a model rejected the API key; check .env"
    return 502, f"the decision could not be completed ({name}: {str(exc)[:160]}). Nothing was posted."


def load_or_create_token(path: Path) -> str:
    """The dashboard's access token survives a restart, so a tab opened before it keeps working. It lives in the (gitignored)
    state folder, like the databases; anyone who can read that folder can already read the .env next to it."""
    try:
        saved = path.read_text(encoding="ascii").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{24,64}", saved):
            return saved
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="ascii")
    except OSError:
        pass  # a read-only folder just means a new token next time
    return token


def serve(settings: Settings, port: int = 8765, open_browser: bool = True) -> int:
    server = QraServer(("127.0.0.1", port), settings, token=load_or_create_token(settings.state_dir / "dashboard.token"))
    url = f"http://127.0.0.1:{server.port}/?t={server.token}"
    print(f"Dashboard: {url}\n(local only; press Ctrl+C to stop)")
    if open_browser:
        import webbrowser
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
