"""Telemetry event store (SQLite, WAL). One row per thing that happened; the dashboard and the CLI read the same rows.

Design rules:
  * Telemetry can NEVER break the service: `emit` swallows every error (and counts what it dropped).
  * No ticket text and no payment data is ever stored: only ids, stages, outcomes, error classes, timings and counts.
  * A stage "span" is stored as one row with the end time and the latency, so a worker-lane timeline can be
    reconstructed (start = ts - latency).

Indexes (nothing here ever scans the table):
  * (run_id, id)             one run's events in order; also the cheap "first and last event of a run"
  * (run_id, kind)           counts and lists by kind inside a run
  * (ticket, id), partial    reverse index from a ticket to everything that happened to it, across runs
  * (kind, error_class, id), partial on faults   reverse index from a failure class to its occurrences
  * runs(seq, run_id)        one row per run, written once when the run starts, so listing runs and finding the
                             latest one costs O(runs), not O(events). Old runs are pruned past KEEP_RUNS.

Event kinds: run_start, run_end, span_start and span (a pipeline stage begins and ends on one worker thread),
http (one helpdesk call),
model (one model call), fault (anything that went wrong or was retried), ticket (a finished ticket).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    run_id     TEXT NOT NULL,
    thread     TEXT,
    kind       TEXT NOT NULL,
    stage      TEXT,
    ticket     TEXT,
    outcome    TEXT,
    error_class TEXT,
    latency_ms INTEGER,
    tokens     INTEGER,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events (run_id, id);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events (run_id, kind);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON events (ticket, id) WHERE ticket IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_fault ON events (error_class, id) WHERE kind = 'fault';
CREATE INDEX IF NOT EXISTS idx_events_fault_run ON events (run_id, error_class) WHERE kind = 'fault';
CREATE TABLE IF NOT EXISTS runs (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE
);
"""

KEEP_RUNS = 50  # older runs (and their events) are removed when a new run starts, so the store stays bounded

KINDS = {"run_start", "run_end", "span_start", "span", "http", "model", "fault", "ticket"}


def prune_runs(db: sqlite3.Connection, keep: int) -> int:
    """Delete everything but the newest `keep` runs. Returns how many runs were removed."""
    old = [r[0] for r in db.execute("SELECT run_id FROM runs ORDER BY seq DESC LIMIT -1 OFFSET ?", (keep,))]
    for run_id in old:
        db.execute("DELETE FROM events WHERE run_id=?", (run_id,))
        db.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
    return len(old)


MAX_DETAIL = 2000


def encode_detail(detail: dict | None) -> str | None:
    """The detail column. Always valid JSON: an oversized detail is replaced by a short document that says it was
    cut (cutting the JSON text itself would leave something the reader cannot parse and would silently lose it)."""
    if not detail:
        return None
    text = json.dumps(detail, ensure_ascii=False, default=str)
    if len(text) <= MAX_DETAIL:
        return text
    return json.dumps({"truncated": True, "chars": len(text), "head": text[:700]}, ensure_ascii=False)


class NullTelemetry:
    """Used when telemetry is off (and in most unit tests): every call is a no-op."""
    run_id = "-"

    def emit(self, kind: str, **fields: Any) -> None: ...

    @contextmanager
    def span(self, stage: str, ticket: str | None = None):
        yield

    def stats(self) -> dict:
        return {"emitted": 0, "dropped": 0}


class Telemetry:
    def __init__(self, path: Path, run_id: str):
        self.run_id = run_id
        self._lock = threading.Lock()
        self.emitted = 0
        self.dropped = 0
        self._db: sqlite3.Connection | None = None
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=5.0)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(_SCHEMA)
            self._register_run()
        except Exception:  # telemetry unavailable: the service carries on without it
            self._db = None

    def _register_run(self) -> None:
        db = self._db
        if db.execute("SELECT 1 FROM runs LIMIT 1").fetchone() is None:  # a store from before the runs table: index it once
            db.execute("INSERT OR IGNORE INTO runs (run_id) SELECT run_id FROM events GROUP BY run_id ORDER BY MIN(id)")
        db.execute("INSERT OR IGNORE INTO runs (run_id) VALUES (?)", (self.run_id,))
        prune_runs(db, KEEP_RUNS)

    def emit(self, kind: str, *, stage: str | None = None, ticket: str | None = None, outcome: str | None = None,
             error_class: str | None = None, latency_ms: int | None = None, tokens: int | None = None,
             detail: dict | None = None) -> None:
        try:
            if self._db is None:
                raise RuntimeError("telemetry store unavailable")
            payload = encode_detail(detail)
            with self._lock:
                self._db.execute(
                    "INSERT INTO events (ts, run_id, thread, kind, stage, ticket, outcome, error_class, latency_ms, tokens, detail) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (time.time(), self.run_id, threading.current_thread().name, kind, stage, ticket, outcome,
                     error_class, latency_ms, tokens, payload))
                self.emitted += 1
        except Exception:
            self.dropped += 1  # never raise from telemetry

    @contextmanager
    def span(self, stage: str, ticket: str | None = None):
        """Time one pipeline stage on the calling worker thread. Failures are recorded, then re-raised."""
        t0 = time.monotonic()
        self.emit("span_start", stage=stage, ticket=ticket)  # lets the dashboard show work that is still running
        try:
            yield
        except BaseException as exc:
            self.emit("span", stage=stage, ticket=ticket, outcome="error", error_class=type(exc).__name__,
                      latency_ms=int((time.monotonic() - t0) * 1000))
            raise
        else:
            self.emit("span", stage=stage, ticket=ticket, outcome="ok", latency_ms=int((time.monotonic() - t0) * 1000))

    def stats(self) -> dict:
        return {"emitted": self.emitted, "dropped": self.dropped}
