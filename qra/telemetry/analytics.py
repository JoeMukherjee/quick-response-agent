"""Reads the event store and builds the numbers the dashboard draws. Pure SQL plus small helpers: no model, no network."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .taxonomy import TAXONOMY, describe

DECISION_TPM_LIMIT = 50_000  # free-tier tokens per minute; the worker pool is paced under this
HELPDESK_RPS_LIMIT = 5       # the helpdesk answers 429 above this


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


class TelemetryReader:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _db(self) -> sqlite3.Connection | None:
        if not self.path.exists():
            return None
        db = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=5.0)
        db.row_factory = sqlite3.Row
        return db

    def runs(self, limit: int = 20) -> list[dict]:
        db = self._db()
        if db is None:
            return []
        try:
            rows = db.execute(
                "SELECT run_id, MIN(ts) AS started, MAX(ts) AS last, "
                "SUM(kind='ticket') AS tickets, SUM(kind='fault' AND error_class NOT IN ('payment_data_redacted')) AS faults "
                "FROM events GROUP BY run_id ORDER BY MIN(id) DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()

    def latest_run(self) -> str | None:
        runs = self.runs(1)
        return runs[0]["run_id"] if runs else None

    def snapshot(self, run_id: str | None = None, lane_window_s: float | None = None) -> dict:
        run_id = run_id or self.latest_run()
        now = time.time()
        empty = {"run": None, "now": now}
        db = self._db()
        if db is None or run_id is None:
            return empty
        try:
            ev = [dict(r) for r in db.execute("SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,))]
        finally:
            db.close()
        if not ev:
            return empty

        starts = [e for e in ev if e["kind"] == "run_start" and e["stage"] is None]
        snap = [e for e in ev if e["kind"] == "run_start" and e["stage"] == "snapshot"]
        end = next((e for e in reversed(ev) if e["kind"] == "run_end"), None)
        meta = json.loads(starts[0]["detail"]) if starts and starts[0]["detail"] else {}
        total = json.loads(snap[0]["detail"])["tickets"] if snap and snap[0]["detail"] else 0
        t_start = starts[0]["ts"] if starts else ev[0]["ts"]
        t_end = end["ts"] if end else now
        status = "running" if end is None else {"ok": "finished", "error": "finished with errors", "fatal": "stopped (invalid key)"}.get(end["outcome"], end["outcome"])

        tickets = [e for e in ev if e["kind"] == "ticket"]
        outcomes: dict[str, int] = {}
        actions: dict[str, int] = {}
        for e in tickets:
            outcomes[e["outcome"]] = outcomes.get(e["outcome"], 0) + 1
            d = json.loads(e["detail"]) if e["detail"] else {}
            if d.get("action"):
                actions[d["action"]] = actions.get(d["action"], 0) + 1
        done = sum(v for k, v in outcomes.items() if k in ("sent", "reconciled", "dry-run", "skipped"))
        elapsed = max(0.001, t_end - t_start)

        spans = [e for e in ev if e["kind"] == "span"]
        stages: dict[str, dict] = {}
        for stage in sorted({e["stage"] for e in spans}):
            lat = [e["latency_ms"] or 0 for e in spans if e["stage"] == stage]
            stages[stage] = {"count": len(lat), "p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95), "max": max(lat)}

        lanes: dict[str, list] = {}
        for e in spans:
            start = e["ts"] - (e["latency_ms"] or 0) / 1000
            if lane_window_s and start < t_end - lane_window_s:
                continue
            lanes.setdefault(e["thread"], []).append(
                {"stage": e["stage"], "ticket": e["ticket"], "start": round(start - t_start, 3),
                 "end": round(e["ts"] - t_start, 3), "outcome": e["outcome"]})

        http = [e for e in ev if e["kind"] == "http"]
        by_status: dict[str, int] = {}
        for e in http:
            by_status[e["outcome"]] = by_status.get(e["outcome"], 0) + 1
        http_lat = [e["latency_ms"] or 0 for e in http]
        per_sec: dict[int, int] = {}
        for e in http:
            per_sec[int(e["ts"] - t_start)] = per_sec.get(int(e["ts"] - t_start), 0) + 1

        models = {}
        for stage in ("judge", "draft", "note"):
            m = [e for e in ev if e["kind"] == "model" and e["stage"] == stage]
            live = [e for e in m if e["outcome"] == "ok"]
            models[stage] = {"calls": len(live), "cached": len(m) - len(live),
                             "tokens": sum(e["tokens"] or 0 for e in live),
                             "p50": _pct([e["latency_ms"] or 0 for e in live], 0.5),
                             "p95": _pct([e["latency_ms"] or 0 for e in live], 0.95)}
        tok_series: dict[int, int] = {}
        for e in ev:
            if e["kind"] == "model" and e["outcome"] == "ok":
                minute = int((e["ts"] - t_start) // 60)
                tok_series[minute] = tok_series.get(minute, 0) + (e["tokens"] or 0)

        fault_rows = [e for e in ev if e["kind"] == "fault"]
        faults = []
        for cls, meta_ in TAXONOMY.items():
            hits = [e for e in fault_rows if e["error_class"] == cls]
            faults.append({"class": cls, **meta_, "count": len(hits), "last": hits[-1]["ts"] - t_start if hits else None})
        for cls in sorted({e["error_class"] for e in fault_rows} - set(TAXONOMY)):  # unknown classes are never hidden
            hits = [e for e in fault_rows if e["error_class"] == cls]
            faults.append({"class": cls, **describe(cls), "count": len(hits), "last": hits[-1]["ts"] - t_start})

        recent = []
        for e in tickets[-40:][::-1]:
            d = json.loads(e["detail"]) if e["detail"] else {}
            recent.append({"ticket": e["ticket"], "outcome": e["outcome"], "ms": e["latency_ms"], "t": round(e["ts"] - t_start, 2), **d})

        return {
            "now": now,
            "run": {"id": run_id, "status": status, "started": t_start, "elapsed": round(elapsed, 2),
                    "workers": meta.get("workers"), "dry_run": meta.get("dry_run"), "total": total},
            "progress": {"done": done, "total": total, "outcomes": outcomes, "actions": actions,
                         "tickets_per_min": round(len(tickets) / elapsed * 60, 1)},
            "stages": stages,
            "lanes": [{"thread": t, "spans": s} for t, s in sorted(lanes.items())],
            "http": {"calls": len(http), "by_status": by_status, "p50": _pct(http_lat, 0.5), "p95": _pct(http_lat, 0.95),
                     "per_second": [{"t": s, "n": per_sec.get(s, 0)} for s in range(0, int(elapsed) + 1)],
                     "limit_rps": HELPDESK_RPS_LIMIT},
            "models": models,
            "tokens_per_minute": {"series": [{"minute": m, "tokens": tok_series.get(m, 0)} for m in range(0, int(elapsed // 60) + 1)],
                                  "limit": DECISION_TPM_LIMIT},
            "faults": faults,
            "recent": recent,
        }
