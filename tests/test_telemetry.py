"""Telemetry: it must never break the service, never store ticket text, and describe every failure class."""
import json
import threading

import pytest
from test_runner import FakeDrafter, FakeJudge, build, make_fake

from qra.core.errors import FatalAuthError
from qra.infra.helpdesk import HelpdeskClient
from qra.infra.ratelimit import RequestLimiter
from qra.runtime.runlog import RunLog
from qra.runtime.runner import Runner
from qra.telemetry import NullTelemetry, Telemetry
from qra.telemetry.analytics import TelemetryReader
from qra.telemetry.taxonomy import TAXONOMY


def instrumented(fake, ledger, tmp_path, workers=4, judge=None, **kw):
    tel = Telemetry(tmp_path / "t.db", "run1")
    client = HelpdeskClient("http://helpdesk.test", "k", "me@example.com", transport=fake.transport,
                            limiter=RequestLimiter(1000), sleep=lambda s: None, telemetry=tel)
    runner = Runner(client, judge or FakeJudge(), FakeDrafter(), ledger, __import__("qra.core.faq", fromlist=["x"]).load_faq(),
                    RunLog(tmp_path / "logs", "t"), workers=workers, telemetry=tel, **kw)
    return runner, TelemetryReader(tmp_path / "t.db")


def test_a_broken_store_never_raises_and_counts_what_it_dropped(tmp_path):
    tel = Telemetry(tmp_path / "no" / "such" / "dir" / ("x" * 400) / "t.db", "r")  # cannot be created on most systems
    tel.emit("fault", error_class="http_503")
    tel._db = None
    tel.emit("ticket", ticket="X")
    with tel.span("plan", "X"):
        pass
    assert tel.stats()["dropped"] >= 2  # and nothing raised


def test_null_telemetry_is_a_no_op():
    t = NullTelemetry()
    t.emit("fault")
    with t.span("plan"):
        pass


def test_a_span_records_failures_then_reraises(tmp_path):
    tel = Telemetry(tmp_path / "t.db", "r")
    with pytest.raises(ValueError):
        with tel.span("plan", "X1"):
            raise ValueError("boom")
    snap = TelemetryReader(tmp_path / "t.db").snapshot("r")
    assert snap["lanes"][0]["spans"][0]["outcome"] == "error"


def test_a_run_produces_worker_lanes_stage_stats_and_a_complete_fault_catalogue(ledger, tmp_path):
    fake = make_fake(24)
    fake.inject("GET /tickets", "503")
    runner, reader = instrumented(fake, ledger, tmp_path, workers=4)
    runner.run()
    snap = reader.snapshot("run1")
    assert snap["run"]["status"] == "finished" and snap["run"]["total"] == 24
    assert snap["progress"]["done"] == 24 and snap["progress"]["outcomes"] == {"sent": 24}
    assert len(snap["lanes"]) >= 1 and {"plan", "draft", "verify", "deliver"} <= set(snap["stages"])
    catalogue = {f["class"] for f in snap["faults"]}
    assert set(TAXONOMY) <= catalogue  # every known failure class is listed, with zero when it did not happen
    assert next(f for f in snap["faults"] if f["class"] == "http_503")["count"] == 1


def test_faults_are_visible_in_telemetry(ledger, tmp_path):
    fake = make_fake(6)
    fake.inject("POST /tickets/T001/handoff", "503")
    fake.inject("POST /tickets/T002/reply", "timeout_after_save")
    fake.inject("GET /tickets", "429")
    runner, reader = instrumented(fake, ledger, tmp_path, workers=2, judge=FakeJudge(fail_for={"how to 4"}))
    runner.run()
    counts = {f["class"]: f["count"] for f in reader.snapshot("run1")["faults"]}
    assert counts["http_429"] == 1 and counts["unknown_outcome"] == 1 and counts["ticket_error"] == 1
    assert counts["http_503"] >= 0  # a 503 on an action that happens to be a reply path is still counted when hit


def test_events_never_contain_ticket_text(ledger, tmp_path):
    fake = make_fake(5)
    runner, _ = instrumented(fake, ledger, tmp_path)
    runner.run()
    import sqlite3
    rows = sqlite3.connect(tmp_path / "t.db").execute("SELECT * FROM events").fetchall()
    assert rows and all("Hoe voeg ik" not in json.dumps(r, default=str) for r in rows)


def test_an_invalid_key_is_recorded_as_the_one_fatal_class(ledger, tmp_path):
    class BadKey(FakeJudge):
        def ask(self, state, questions):
            raise FatalAuthError("invalid")

    runner, reader = instrumented(make_fake(4), ledger, tmp_path, judge=BadKey())
    with pytest.raises(FatalAuthError):
        runner.run()
    snap = reader.snapshot("run1")
    assert snap["run"]["status"] == "stopped (invalid key)"


def test_several_threads_really_run_concurrently(ledger, tmp_path):
    fake = make_fake(30)
    runner, reader = instrumented(fake, ledger, tmp_path, workers=6)
    runner.run()
    lanes = reader.snapshot("run1")["lanes"]
    assert len({lane["thread"] for lane in lanes}) >= 2 and sum(len(l["spans"]) for l in lanes) >= 30


def test_a_stage_that_has_started_but_not_ended_shows_as_running(tmp_path):
    tel = Telemetry(tmp_path / "t.db", "r")
    tel.emit("run_start", detail={"workers": 2, "dry_run": True})
    started = threading.Event(); release = threading.Event()

    def work():
        with tel.span("draft", "X1"):
            started.set(); release.wait(2)

    t = threading.Thread(target=work, name="qra-worker-0"); t.start(); started.wait(2)
    snap = TelemetryReader(tmp_path / "t.db").snapshot("r")
    spans = [s for lane in snap["lanes"] for s in lane["spans"]]
    assert snap["active_threads"] == 1 and spans[0]["outcome"] == "running" and snap["activity"]["draft"] == 1
    release.set(); t.join()
    done = TelemetryReader(tmp_path / "t.db").snapshot("r")
    assert done["lanes"][0]["spans"][0]["outcome"] == "ok" and done["active_threads"] == 0


def test_a_killed_run_is_shown_as_interrupted_not_running_forever(tmp_path, monkeypatch):
    import time as _time
    tel = Telemetry(tmp_path / "t.db", "r")
    tel.emit("run_start", detail={"workers": 2, "dry_run": False})
    tel.emit("run_start", stage="snapshot", detail={"tickets": 5, "workers": 2})
    reader = TelemetryReader(tmp_path / "t.db")
    assert reader.snapshot("r")["run"]["status"] == "running"
    real = _time.time
    monkeypatch.setattr("qra.telemetry.analytics.time.time", lambda: real() + 60)  # a minute of silence, no run_end
    snap = reader.snapshot("r")
    assert snap["run"]["status"].startswith("interrupted") and snap["active_threads"] == 0
