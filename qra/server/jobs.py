"""Background jobs started from the web UI. One job at a time; every failure becomes a message, never a crash."""
from __future__ import annotations

import threading
from dataclasses import replace
import time
from urllib.parse import urlparse
from typing import Any, Callable

from qra.core.config import Settings
from qra.runtime import service


def parse_api(settings: Settings, api) -> tuple[str, str] | None:
    """The ticket API chosen in the dashboard: (url, key), or None to use the saved one. https only (plain http only for this
    machine). The saved key is NEVER sent to a different address: a different URL needs its own key, typed for this job."""
    if not api:
        return None
    if not isinstance(api, dict):
        raise ValueError("api must be an object with a url and, for another address, a key")
    url, key = str(api.get("url") or "").strip().rstrip("/"), str(api.get("key") or "").strip()
    if not url and not key:
        return None
    url = url or settings.helpdesk_url.rstrip("/")
    host = urlparse(url).hostname or ""
    if len(url) > 200 or len(key) > 200 or not (url.startswith("https://") or (url.startswith("http://") and host in ("localhost", "127.0.0.1", "::1"))):
        raise ValueError("the ticket API must be an https:// address (http:// only for localhost)")
    if url != settings.helpdesk_url.rstrip("/") and not key:
        raise ValueError("enter the API key for that address; the saved key is never sent to a different one")
    return url, key or settings.helpdesk_key


class JobManager:
    def __init__(self, settings: Settings, runner: Callable[..., Any] | None = None):
        self.s = settings
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state: dict[str, Any] = {"status": "idle"}
        self.last: dict[str, Any] | None = None
        self._svc = runner or service  # injectable for tests

    def status(self) -> dict:
        with self._lock:
            return {**self.state, "last": self.last}

    def start(self, kind: str, *, workers: int = 4, dry_run: bool = True, retrieval: str = "exhaustive",
              judge: str | None = None, suite: str = "all", api: tuple[str, str] | None = None) -> tuple[bool, str]:
        judge = judge or self.s.judge_backend  # the configured default decision model
        if suite not in ("nordavo", "ours", "all"):
            return False, f"unknown test set {suite!r}"
        if kind not in ("run", "eval", "reset", "final"):
            return False, f"unknown job {kind!r}"
        if retrieval not in ("exhaustive", "cascade"):
            return False, f"unknown retrieval method {retrieval!r}"
        if judge not in ("gemini", "typesafe"):
            return False, f"unknown decision model {judge!r}"
        if judge == "typesafe" and not self.s.typesafe_api_key:
            return False, "TYPESAFE_API_KEY is not set in .env"
        workers = max(1, min(int(workers), 8))
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False, "a job is already running"
            run_id = service.new_run_id()
            self.state = {"status": "running", "kind": kind, "run_id": run_id, "dry_run": dry_run, "workers": workers,
                          "retrieval": retrieval, "judge": judge, "started": time.time(),
                          "api": (api[0] if api else self.s.helpdesk_url)}  # the address only, never the key
            self._thread = threading.Thread(target=self._work, args=(kind, run_id, workers, dry_run, retrieval, judge, suite, api), daemon=True,
                                            name=f"qra-job-{kind}")
            self._thread.start()
        return True, run_id

    def _work(self, kind: str, run_id: str, workers: int, dry_run: bool, retrieval: str = "exhaustive",
              judge: str = "gemini", suite: str = "all", api: tuple[str, str] | None = None) -> None:
        try:
            settings = replace(self.s, retrieval=retrieval, judge_backend=judge)
            if api:
                settings = replace(settings, helpdesk_url=api[0], helpdesk_key=api[1])  # the choices for THIS job only; the server defaults are untouched
            if kind == "final":  # the submission run: re-open every practice ticket, then answer each one once, live
                result = self._svc.execute_reset(settings)
                if not result.exit_code:
                    result = self._svc.execute_run(settings, workers=workers, dry_run=False, run_id=run_id)
            elif kind == "run":
                result = self._svc.execute_run(settings, workers=workers, dry_run=dry_run, run_id=run_id)
            elif kind == "eval":
                result = self._svc.execute_eval(settings, suite=suite)
            else:
                result = self._svc.execute_reset(self.s)
            outcome = {"kind": kind, "run_id": run_id, "exit_code": result.exit_code, "message": result.message,
                       "summary": result.summary if kind != "eval" else _eval_digest(result.summary)}
        except Exception as exc:  # defence in depth: a job can never take the server down
            outcome = {"kind": kind, "run_id": run_id, "exit_code": 1,
                       "message": f"unexpected error ({type(exc).__name__}); logged, the service is still running",
                       "summary": {}}
        with self._lock:
            self.last = {**outcome, "finished": time.time()}
            self.state = {"status": "idle"}


def _eval_digest(summary: dict) -> dict:
    if not summary:
        return {}
    return {name: block["metrics"] for name, block in summary.get("results", {}).items()} | {
        "live_calls": summary.get("live_calls"), "cache_hits": summary.get("cache_hits"), "sources": summary.get("sources", {})}
