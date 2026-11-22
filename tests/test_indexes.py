"""Indexing guarantees: nothing scans the whole store, the reader only reads what is new, and old data is bounded."""
import sqlite3

import pytest

from qra.telemetry import events as ev
from qra.telemetry.analytics import TelemetryReader
from qra.telemetry.events import Telemetry


def _plan(db_path, sql, args=()):
    db = sqlite3.connect(db_path)
    try:
        return " | ".join(r[3] for r in db.execute("EXPLAIN QUERY PLAN " + sql, args))
    finally:
        db.close()


def _emit_run(path, run_id, tickets=3, fault=None):
    t = Telemetry(path, run_id)
    t.emit("run_start", detail={"workers": 2, "dry_run": True})
    for i in range(tickets):
        with t.span("plan", ticket=f"K{i}"):
            pass
        t.emit("ticket", ticket=f"K{i}", outcome="dry-run", latency_ms=5, detail={"action": "reply"})
    if fault:
        t.emit("fault", stage="draft", ticket="K0", error_class=fault, outcome="retried")
    t.emit("run_end", outcome="ok")
    return t


def test_runs_and_latest_run_come_from_the_runs_table_in_order(tmp_path):
    db = tmp_path / "t.db"
    for name in ("first", "second", "third"):
        _emit_run(db, name)
    reader = TelemetryReader(db)
    assert reader.latest_run() == "third"
    assert [r["run_id"] for r in reader.runs()] == ["third", "second", "first"]
    assert reader.runs(1)[0]["tickets"] == 3
    assert "runs" in _plan(db, "SELECT run_id FROM runs ORDER BY seq DESC LIMIT 1")


def test_listing_runs_never_scans_the_event_table(tmp_path):
    db = tmp_path / "t.db"
    _emit_run(db, "r1", fault="draft_error")
    for sql, args in [("SELECT ts FROM events WHERE run_id=? ORDER BY id LIMIT 1", ("r1",)),
                      ("SELECT ts FROM events WHERE run_id=? ORDER BY id DESC LIMIT 1", ("r1",)),
                      ("SELECT COUNT(*) FROM events WHERE run_id=? AND kind='ticket'", ("r1",)),
                      ("SELECT COUNT(*) FROM events WHERE run_id=? AND kind='fault' AND error_class NOT IN (?)", ("r1", "x")),
                      ("SELECT id FROM events WHERE run_id=? AND id>? ORDER BY id", ("r1", 5)),
                      ("SELECT MAX(id) FROM events WHERE run_id=?", ("r1",))]:
        plan = _plan(db, sql, args)
        assert all(seg.startswith("SEARCH") for seg in plan.split(" | ")), plan  # a SCAN would read every row


def test_a_ticket_and_a_failure_class_are_found_through_their_own_indexes(tmp_path):
    db = tmp_path / "t.db"
    _emit_run(db, "r1", fault="draft_error")
    _emit_run(db, "r2")
    assert "idx_events_ticket" in _plan(db, "SELECT id FROM events WHERE ticket=? ORDER BY id LIMIT 5", ("K1",))
    assert "idx_events_fault" in _plan(db, "SELECT id FROM events WHERE kind='fault' AND error_class=? ORDER BY id DESC LIMIT 5", ("x",))
    reader = TelemetryReader(db)
    trace = reader.trace("K1")
    assert [e["run_id"] for e in trace] == ["r1", "r1", "r1", "r2", "r2", "r2"]  # across runs, oldest first
    assert {e["kind"] for e in trace} == {"span_start", "span", "ticket"}
    assert [e["run_id"] for e in reader.trace("K1", run_id="r2")] == ["r2"] * 3
    hits = reader.faults_of("draft_error")
    assert len(hits) == 1 and hits[0]["ticket"] == "K0" and hits[0]["run_id"] == "r1"
    assert reader.trace("nope") == [] and reader.faults_of("nope") == []


def test_the_reader_loads_only_new_events_and_sees_them(tmp_path):
    db = tmp_path / "t.db"
    t = Telemetry(db, "live")
    t.emit("run_start", detail={"workers": 1, "dry_run": False})
    t.emit("run_start", stage="snapshot", detail={"tickets": 5})
    reader = TelemetryReader(db)
    assert reader.snapshot("live")["progress"]["done"] == 0
    first_last_id = reader._runs["live"].last_id
    t.emit("ticket", ticket="A", outcome="sent", latency_ms=3, detail={"action": "reply"})
    snap = reader.snapshot("live")
    assert snap["progress"]["done"] == 1 and snap["progress"]["total"] == 5 and snap["run"]["status"] == "running"
    assert reader._runs["live"].last_id > first_last_id
    assert len(reader._runs["live"].events) == 3  # nothing was read twice
    reader.snapshot("live")
    assert len(reader._runs["live"].events) == 3


def test_a_stage_that_started_and_never_ended_is_running_then_cleared(tmp_path):
    db = tmp_path / "t.db"
    t = Telemetry(db, "r")
    t.emit("run_start", detail={"workers": 1, "dry_run": False})
    t.emit("span_start", stage="plan", ticket="A")
    reader = TelemetryReader(db)
    lanes = reader.snapshot("r")["lanes"]
    assert lanes[0]["spans"][0]["outcome"] == "running"
    t.emit("span", stage="plan", ticket="A", outcome="ok", latency_ms=10)
    spans = reader.snapshot("r")["lanes"][0]["spans"]
    assert [s["outcome"] for s in spans] == ["ok"]


def test_a_recreated_store_makes_the_reader_start_the_run_over(tmp_path):
    db = tmp_path / "t.db"
    _emit_run(db, "same", tickets=4)
    reader = TelemetryReader(db)
    assert reader.snapshot("same")["progress"]["done"] == 4
    conn = sqlite3.connect(db)  # wipe the store the way deleting state/ would: ids start from 1 again
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM sqlite_sequence")
    conn.execute("DELETE FROM runs")
    conn.commit()
    conn.close()
    _emit_run(db, "same", tickets=1)  # ids start from 1 again, below what the reader had indexed
    assert reader.snapshot("same")["progress"]["done"] == 1


def test_only_the_newest_runs_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "KEEP_RUNS", 3)
    db = tmp_path / "t.db"
    for i in range(6):
        _emit_run(db, f"run{i}")
    reader = TelemetryReader(db)
    assert [r["run_id"] for r in reader.runs(10)] == ["run5", "run4", "run3"]
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(DISTINCT run_id) FROM events").fetchone()[0] == 3  # the events went with them
    assert reader.trace("K0", run_id="run0") == []


def test_a_store_from_before_the_runs_table_is_migrated_and_still_readable(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, run_id TEXT NOT NULL,
        thread TEXT, kind TEXT NOT NULL, stage TEXT, ticket TEXT, outcome TEXT, error_class TEXT, latency_ms INTEGER,
        tokens INTEGER, detail TEXT);""")
    conn.execute("INSERT INTO events (ts, run_id, thread, kind, outcome) VALUES (1.0, 'legacy', 'main', 'ticket', 'sent')")
    conn.commit()
    conn.close()
    reader = TelemetryReader(db)  # read-only against a store with no runs table: falls back to the slower query
    assert reader.latest_run() == "legacy" and reader.runs()[0]["tickets"] == 1
    _emit_run(db, "new")  # the first writer builds the runs table and indexes the old run once
    assert [r["run_id"] for r in TelemetryReader(db).runs()] == ["new", "legacy"]


def test_telemetry_that_cannot_open_never_raises(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    t = Telemetry(blocked / "t.db", "r")  # parent is a file: the store cannot be created
    t.emit("ticket", ticket="A")  # swallowed and counted
    assert t.stats()["dropped"] == 1


def test_a_huge_run_ships_only_recent_history_to_the_dashboard(tmp_path):
    from qra.telemetry import analytics
    db = tmp_path / "t.db"
    t = Telemetry(db, "big")
    t.emit("run_start", detail={"workers": 2, "dry_run": True})
    base = 1_000_000.0
    conn = t._db
    conn.execute("BEGIN")
    for i in range(2000):
        conn.execute("INSERT INTO events (ts, run_id, thread, kind, stage, ticket, outcome, latency_ms) VALUES (?, 'big', 'w1', 'span', 'plan', ?, 'ok', 50)",
                     (base + i * 2, f"T{i}"))
        conn.execute("INSERT INTO events (ts, run_id, thread, kind, stage, ticket, outcome, latency_ms) VALUES (?, 'big', 'w1', 'http', 'get', ?, '200', 5)",
                     (base + i * 2, f"T{i}"))
    conn.execute("INSERT INTO events (ts, run_id, thread, kind, outcome) VALUES (?, 'big', 'main', 'run_end', 'ok')", (base + 5000,))
    conn.execute("COMMIT")
    snap = TelemetryReader(db).snapshot("big")
    assert sum(len(lane["spans"]) for lane in snap["lanes"]) == analytics.MAX_LANE_SPANS
    assert len(snap["http"]["per_second"]) <= analytics.MAX_RATE_SECONDS + 1
    assert snap["stages"]["plan"]["count"] == 2000  # the numbers still cover the whole run; only the drawn history is bounded
    assert snap["http"]["calls"] == 2000
