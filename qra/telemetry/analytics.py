"""Reads the event store and builds the numbers the dashboard draws. No model, no network.

The dashboard asks for a fresh snapshot every 700 ms, so the reader must not redo the work each time. It keeps an
in-memory index per run, filled incrementally: each call reads only the events added since the last call
(`WHERE run_id=? AND id>?`, an index range), parses each event's detail once, and files the event under its kind,
stage, failure class and ticket. A snapshot then walks only the lists it needs, and stages that have started but not
finished are tracked as events arrive instead of being recomputed by replaying the run.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .taxonomy import TAXONOMY, describe

DECISION_TPM_LIMIT = 50_000  # free-tier tokens per minute; the worker pool is paced under this
HELPDESK_RPS_LIMIT = 5       # the helpdesk answers 429 above this
STALE_AFTER_S = 20           # a run silent for this long without a run_end was killed
CACHED_RUNS = 4              # run indexes kept in memory (the live run and a few recent ones)
MAX_LANE_SPANS = 800        # the worker lanes show recent history; a huge run must not ship every span every 700 ms
MAX_RATE_SECONDS = 300      # per-second request chart: the most recent five minutes
MAX_TOKEN_MINUTES = 90      # tokens-per-minute chart: the most recent ninety minutes
IGNORED_FAULTS = ("payment_data_redacted",)  # a redaction is the system working, not a fault

_COLUMNS = "id, ts, run_id, thread, kind, stage, ticket, outcome, error_class, latency_ms, tokens, detail"


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


class _RunIndex:
    """Everything known about one run, indexed so a snapshot never rescans or re-parses."""

    def __init__(self) -> None:
        self.last_id = 0
        self.events: list[dict] = []
        self.by_kind: dict[str, list[dict]] = {}
        self.models: dict[str, list[dict]] = {}          # model stage -> its events
        self.fault_by_class: dict[str, list[dict]] = {}  # failure class -> its events
        self.by_ticket: dict[str, list[dict]] = {}
        self.open_spans: dict[tuple, dict] = {}          # (thread, stage, ticket) of stages started and not yet finished

    def add(self, row: sqlite3.Row) -> None:
        e = dict(row)
        try:
            e["d"] = json.loads(e["detail"]) if e["detail"] else {}
        except ValueError:
            e["d"] = {}
        self.last_id = e["id"]
        self.events.append(e)
        kind = e["kind"]
        self.by_kind.setdefault(kind, []).append(e)
        if e["ticket"]:
            self.by_ticket.setdefault(e["ticket"], []).append(e)
        if kind == "model":
            self.models.setdefault(e["stage"], []).append(e)
        elif kind == "fault":
            self.fault_by_class.setdefault(e["error_class"] or "unknown", []).append(e)
        elif kind == "span_start":
            self.open_spans[(e["thread"], e["stage"], e["ticket"])] = e
        elif kind == "span":
            self.open_spans.pop((e["thread"], e["stage"], e["ticket"]), None)


class TelemetryReader:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._runs: "OrderedDict[str, _RunIndex]" = OrderedDict()

    def _db(self) -> sqlite3.Connection | None:
        if not self.path.exists():
            return None
        db = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=5.0)
        db.row_factory = sqlite3.Row
        return db

    # ---- listing runs: O(runs), independent of how many events exist ---------------------------------------------
    def runs(self, limit: int = 20) -> list[dict]:
        db = self._db()
        if db is None:
            return []
        try:
            try:
                ids = [r[0] for r in db.execute("SELECT run_id FROM runs ORDER BY seq DESC LIMIT ?", (limit,))]
            except sqlite3.OperationalError:  # a store from before the runs table existed
                ids = [r[0] for r in db.execute(
                    "SELECT run_id FROM events GROUP BY run_id ORDER BY MIN(id) DESC LIMIT ?", (limit,))]
            out = []
            for run_id in ids:
                first = db.execute("SELECT ts FROM events WHERE run_id=? ORDER BY id LIMIT 1", (run_id,)).fetchone()
                last = db.execute("SELECT ts FROM events WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
                tickets = db.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND kind='ticket'", (run_id,)).fetchone()[0]
                marks = ",".join("?" * len(IGNORED_FAULTS))
                faults = db.execute(
                    f"SELECT COUNT(*) FROM events WHERE run_id=? AND kind='fault' AND error_class NOT IN ({marks})",
                    (run_id, *IGNORED_FAULTS)).fetchone()[0]
                if first:
                    out.append({"run_id": run_id, "started": first[0], "last": last[0], "tickets": tickets, "faults": faults})
            return out
        finally:
            db.close()

    def latest_run(self) -> str | None:
        db = self._db()
        if db is None:
            return None
        try:
            try:
                row = db.execute("SELECT run_id FROM runs ORDER BY seq DESC LIMIT 1").fetchone()
            except sqlite3.OperationalError:
                row = db.execute("SELECT run_id FROM events ORDER BY id DESC LIMIT 1").fetchone()
            return row[0] if row else None
        finally:
            db.close()

    # ---- reverse lookups through the ticket and failure-class indexes ---------------------------------------------
    def trace(self, ticket: str, run_id: str | None = None, limit: int = 200) -> list[dict]:
        """Everything that happened to one ticket, oldest first (reverse index: ticket -> events)."""
        db = self._db()
        if db is None:
            return []
        try:
            sql = f"SELECT {_COLUMNS} FROM events WHERE ticket=?" + (" AND run_id=?" if run_id else "") + " ORDER BY id LIMIT ?"
            args = (ticket, run_id, limit) if run_id else (ticket, limit)
            return [self._public(dict(r)) for r in db.execute(sql, args)]
        finally:
            db.close()

    def faults_of(self, error_class: str, limit: int = 50) -> list[dict]:
        """The most recent occurrences of one failure class, newest first (reverse index: class -> events)."""
        db = self._db()
        if db is None:
            return []
        try:
            return [self._public(dict(r)) for r in db.execute(
                f"SELECT {_COLUMNS} FROM events WHERE kind='fault' AND error_class=? ORDER BY id DESC LIMIT ?",
                (error_class, limit))]
        finally:
            db.close()

    @staticmethod
    def _public(e: dict) -> dict:
        try:
            e["detail"] = json.loads(e["detail"]) if e["detail"] else None
        except ValueError:
            pass
        return e

    # ---- incremental load ---------------------------------------------------------------------------------------
    def _index(self, db: sqlite3.Connection, run_id: str) -> _RunIndex:
        idx = self._runs.get(run_id)
        newest = db.execute("SELECT MAX(id) FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
        if idx is not None and (newest is None or newest < idx.last_id):
            idx = None  # the store was pruned or recreated under us: start this run over
        if idx is None:
            idx = _RunIndex()
        for row in db.execute(f"SELECT {_COLUMNS} FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, idx.last_id)):
            idx.add(row)
        self._runs[run_id] = idx
        self._runs.move_to_end(run_id)
        while len(self._runs) > CACHED_RUNS:
            self._runs.popitem(last=False)
        return idx

    def snapshot(self, run_id: str | None = None, lane_window_s: float | None = None) -> dict:
        run_id = run_id or self.latest_run()
        now = time.time()
        empty = {"run": None, "now": now}
        db = self._db()
        if db is None or run_id is None:
            return empty
        try:
            with self._lock:
                idx = self._index(db, run_id)
                if not idx.events:
                    return empty
                return self._build(idx, run_id, now, lane_window_s)
        finally:
            db.close()

    def _build(self, idx: _RunIndex, run_id: str, now: float, lane_window_s: float | None) -> dict:
        ev, kinds = idx.events, idx.by_kind
        starts = [e for e in kinds.get("run_start", []) if e["stage"] is None]
        snap = [e for e in kinds.get("run_start", []) if e["stage"] == "snapshot"]
        end = kinds["run_end"][-1] if kinds.get("run_end") else None
        meta = starts[0]["d"] if starts else {}
        total = snap[0]["d"].get("tickets", 0) if snap else 0
        t_start = starts[0]["ts"] if starts else ev[0]["ts"]
        # a run with no end event and no activity for a while was killed: show it as interrupted, not as running
        stale = end is None and now - ev[-1]["ts"] > STALE_AFTER_S
        live = end is None and not stale
        t_end = end["ts"] if end else (ev[-1]["ts"] if stale else now)
        status = "interrupted (process stopped)" if stale else "running" if end is None else {"ok": "finished", "error": "finished with errors", "fatal": "stopped (invalid key)"}.get(end["outcome"], end["outcome"])

        tickets = kinds.get("ticket", [])
        outcomes: dict[str, int] = {}
        actions: dict[str, int] = {}
        resampled = 0
        for e in tickets:
            outcomes[e["outcome"]] = outcomes.get(e["outcome"], 0) + 1
            if e["d"].get("action"):
                actions[e["d"]["action"]] = actions.get(e["d"]["action"], 0) + 1
            if e["d"].get("resampled"):
                resampled += 1
        done = sum(v for k, v in outcomes.items() if k in ("sent", "reconciled", "dry-run", "skipped"))
        elapsed = max(0.001, t_end - t_start)

        spans = kinds.get("span", [])
        running = list(idx.open_spans.values()) if live else []
        lat_by_stage: dict[str, list[int]] = {}
        for e in spans:
            lat_by_stage.setdefault(e["stage"], []).append(e["latency_ms"] or 0)
        stages = {s: {"count": len(lat), "p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95), "max": max(lat)}
                  for s, lat in sorted(lat_by_stage.items(), key=lambda kv: str(kv[0]))}

        lanes: dict[str, list] = {}
        for e in running:
            lanes.setdefault(e["thread"], []).append(
                {"stage": e["stage"], "ticket": e["ticket"], "start": round(e["ts"] - t_start, 3),
                 "end": round(now - t_start, 3), "outcome": "running"})
        for e in spans[-MAX_LANE_SPANS:]:
            start = e["ts"] - (e["latency_ms"] or 0) / 1000
            if lane_window_s and start < t_end - lane_window_s:
                continue
            lanes.setdefault(e["thread"], []).append(
                {"stage": e["stage"], "ticket": e["ticket"], "start": round(start - t_start, 3),
                 "end": round(e["ts"] - t_start, 3), "outcome": e["outcome"]})

        http = kinds.get("http", [])
        by_status: dict[str, int] = {}
        per_sec: dict[int, int] = {}
        for e in http:
            by_status[e["outcome"]] = by_status.get(e["outcome"], 0) + 1
            sec = int(e["ts"] - t_start)
            per_sec[sec] = per_sec.get(sec, 0) + 1
        http_lat = [e["latency_ms"] or 0 for e in http]

        models = {}
        for stage in ("judge", "draft", "note", "opinion"):
            m = idx.models.get(stage, [])
            ok = [e for e in m if e["outcome"] == "ok"]
            models[stage] = {"calls": len(ok), "cached": len(m) - len(ok),
                             "tokens": sum(e["tokens"] or 0 for e in ok),
                             "p50": _pct([e["latency_ms"] or 0 for e in ok], 0.5),
                             "p95": _pct([e["latency_ms"] or 0 for e in ok], 0.95)}
        tok_series: dict[int, int] = {}
        for stage_events in idx.models.values():
            for e in stage_events:
                if e["outcome"] == "ok":
                    minute = int((e["ts"] - t_start) // 60)
                    tok_series[minute] = tok_series.get(minute, 0) + (e["tokens"] or 0)

        faults = []
        for cls, meta_ in TAXONOMY.items():
            hits = idx.fault_by_class.get(cls, [])
            faults.append({"class": cls, **meta_, "count": len(hits), "last": hits[-1]["ts"] - t_start if hits else None})
        for cls in sorted(set(idx.fault_by_class) - set(TAXONOMY)):  # unknown classes are never hidden
            hits = idx.fault_by_class[cls]
            faults.append({"class": cls, **describe(cls), "count": len(hits), "last": hits[-1]["ts"] - t_start})

        # "what is happening right now": events in the last two seconds of a live run (nothing once it has ended)
        activity: dict[str, int] = {}
        if live:
            for kind in ("http", "model"):
                for e in reversed(kinds.get(kind, [])):  # ids are in time order, so stop at the first old one
                    if now - e["ts"] > 2.0:
                        break
                    if e["stage"]:
                        activity[e["stage"]] = activity.get(e["stage"], 0) + 1
            for e in running:  # stages that are running right now
                activity[e["stage"]] = activity.get(e["stage"], 0) + 1
        active_threads = len({e["thread"] for e in running}) if live else 0
        ticket_ms = [e["latency_ms"] for e in tickets if e["latency_ms"]]

        recent = [{"ticket": e["ticket"], "outcome": e["outcome"], "ms": e["latency_ms"], "t": round(e["ts"] - t_start, 2), **e["d"]}
                  for e in tickets[-40:][::-1]]

        return {
            "now": now,
            "run": {"id": run_id, "status": status, "started": t_start, "elapsed": round(elapsed, 2),
                    "workers": meta.get("workers"), "dry_run": meta.get("dry_run"), "total": total},
            "progress": {"done": done, "total": total, "outcomes": outcomes, "actions": actions, "resampled": resampled,
                         "tickets_per_min": round(len(tickets) / elapsed * 60, 1)},
            "stages": stages,
            "lanes": [{"thread": t, "spans": s} for t, s in sorted(lanes.items())],
            "http": {"calls": len(http), "by_status": by_status, "p50": _pct(http_lat, 0.5), "p95": _pct(http_lat, 0.95),
                     "per_second": [{"t": s, "n": per_sec.get(s, 0)} for s in range(max(0, int(elapsed) - MAX_RATE_SECONDS), int(elapsed) + 1)],
                     "limit_rps": HELPDESK_RPS_LIMIT},
            "models": models,
            "tokens_per_minute": {"series": [{"minute": m, "tokens": tok_series.get(m, 0)} for m in range(max(0, int(elapsed // 60) - MAX_TOKEN_MINUTES), int(elapsed // 60) + 1)],
                                  "limit": DECISION_TPM_LIMIT},
            "faults": faults,
            "activity": activity, "active_threads": active_threads,
            "ticket_ms": {"p50": _pct(ticket_ms, 0.5), "p95": _pct(ticket_ms, 0.95)},
            "recent": recent,
        }
