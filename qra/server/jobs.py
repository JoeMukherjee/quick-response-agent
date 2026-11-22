"""Background jobs started from the web UI. One job at a time; every failure becomes a message, never a crash."""
from __future__ import annotations

import threading
from dataclasses import replace
import time
from typing import Any, Callable

from qra.core.config import Settings
from qra.runtime import service


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
              judge: str = "gemini") -> tuple[bool, str]:
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
                          "retrieval": retrieval, "judge": judge, "started": time.time()}
            self._thread = threading.Thread(target=self._work, args=(kind, run_id, workers, dry_run, retrieval, judge), daemon=True,
                                            name=f"qra-job-{kind}")
            self._thread.start()
        return True, run_id

    def _work(self, kind: str, run_id: str, workers: int, dry_run: bool, retrieval: str = "exhaustive",
              judge: str = "gemini") -> None:
        try:
            settings = replace(self.s, retrieval=retrieval, judge_backend=judge)  # the choices for THIS job only; the server defaults are untouched
            if kind == "final":  # the submission run: re-open every practice ticket, then answer each one once, live
                result = self._svc.execute_reset(settings)
                if not result.exit_code:
                    result = self._svc.execute_run(settings, workers=workers, dry_run=False, run_id=run_id)
            elif kind == "run":
                result = self._svc.execute_run(settings, workers=workers, dry_run=dry_run, run_id=run_id)
            elif kind == "eval":
                result = self._svc.execute_eval(settings)
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
        "live_calls": summary.get("live_calls"), "cache_hits": summary.get("cache_hits")}
