"""Local web server for the live dashboard and bench controls (stdlib only, no build step, works offline).

Security model (the page can trigger helpdesk actions, so it is locked down):
  * binds to 127.0.0.1 only;
  * a random token is printed/opened once, then kept in an HttpOnly SameSite=Strict cookie;
  * every request must carry the cookie (or X-QRA-Token) and a Host header for the loopback address (blocks DNS rebinding);
  * every POST needs a custom header (forces a CORS preflight the server never grants) and a same-origin Origin;
  * a real run and a reset need an explicit typed confirmation; dry-run and eval are the defaults.
The server never raises to the client: handler errors become a JSON 500 and the process keeps serving.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from qra import __version__
from qra.core.config import Settings
from qra.runtime.service import telemetry_path
from qra.server.jobs import JobManager
from qra.telemetry.analytics import DECISION_TPM_LIMIT, HELPDESK_RPS_LIMIT, TelemetryReader

log = logging.getLogger("qra.server")
STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "qra_token"


class QraServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, settings: Settings, token: str | None = None, jobs: JobManager | None = None):
        super().__init__(addr, Handler)
        self.settings = settings
        self.token = token or secrets.token_urlsafe(24)
        self.jobs = jobs or JobManager(settings)
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
    def _send(self, code: int, body: bytes, ctype: str = "application/json", headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

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
                                        "models": {"judge": self.server.settings.model_judge, "draft": self.server.settings.model_draft}})
            if url.path == "/api/runs":
                return self._json(200, {"runs": self.server.reader.runs()})
            if url.path == "/api/snapshot":
                return self._json(200, self.server.reader.snapshot(run))
            if url.path == "/api/job":
                return self._json(200, self.server.jobs.status())
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
        self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")

    def _static(self, name: str) -> None:
        path = (STATIC / name).resolve()
        if STATIC.resolve() not in path.parents or not path.is_file():
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text/") or "javascript" in ctype else ""))

    def _stream(self, run: str | None) -> None:
        """Server-sent events: a fresh snapshot every 700 ms until the client goes away."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                payload = {"snapshot": self.server.reader.snapshot(run), "job": self.server.jobs.status()}
                self.wfile.write(b"data: " + json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8") + b"\n\n")
                self.wfile.flush()
                time.sleep(0.7)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # ---- POST ----------------------------------------------------------------------------------------
    def do_POST(self) -> None:
        try:
            if urlparse(self.path).path != "/api/job":
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            if not self._guard(post=True):
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(length, 4096)) or b"{}")
            kind = body.get("kind")
            dry = bool(body.get("dry_run", True))
            if kind == "run" and not dry and body.get("confirm") != "RUN":
                return self._json(400, {"error": "a real run needs the typed confirmation RUN"})
            if kind == "reset" and body.get("confirm") != "RESET":
                return self._json(400, {"error": "reset needs the typed confirmation RESET"})
            ok, info = self.server.jobs.start(kind, workers=body.get("workers", 4), dry_run=dry)
            return self._json(200 if ok else 409, {"ok": ok, "run_id" if ok else "error": info})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            log.exception("POST %s failed", self.path)
            try:
                self._json(500, {"error": f"internal error ({type(exc).__name__})"})
            except Exception:
                pass


def serve(settings: Settings, port: int = 8765, open_browser: bool = True) -> int:
    server = QraServer(("127.0.0.1", port), settings)
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
