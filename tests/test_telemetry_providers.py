"""Telemetry for more than one decision provider: what is recorded, and that the reader parses it back correctly."""
import json
import sqlite3

import httpx

from qra.decision.judge import Judge
from qra.decision.typesafe import make_create
from qra.telemetry.analytics import DECISION_TPM_LIMIT, TelemetryReader, token_limit
from qra.telemetry.events import MAX_DETAIL, Telemetry, encode_detail

REPLY = {"model": "jev-1.14.0", "usage": {"input_tokens": 1909, "output_tokens": 526},
         "answers": {"refund_or_dispute": {"type": "noul", "noul": 0.98}}}


def typesafe_judge(tel, cache=None):
    create = make_create("k", client=httpx.Client(base_url="https://api.typesafe.ai", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=REPLY))))
    return Judge(create, "jev-1.14.0", cache, telemetry=tel, provider="TypeSafe")


# ---- an oversized detail must never become unparseable ----------------------------------------------------------

def test_a_small_detail_is_stored_exactly():
    assert json.loads(encode_detail({"a": 1, "b": "x"})) == {"a": 1, "b": "x"}
    assert encode_detail(None) is None and encode_detail({}) is None


def test_an_oversized_detail_is_still_valid_json_and_says_it_was_cut():
    stored = encode_detail({"error": "x" * 5000, "ticket": "T1"})
    assert len(stored) <= MAX_DETAIL
    doc = json.loads(stored)  # the old behaviour cut the text mid-string and this raised
    assert doc["truncated"] is True and doc["chars"] > MAX_DETAIL and doc["head"]


def test_an_oversized_detail_round_trips_through_the_store_and_the_reader(tmp_path):
    t = Telemetry(tmp_path / "t.db", "r")
    t.emit("run_start", detail={"workers": 1, "dry_run": True})
    t.emit("ticket", ticket="T1", outcome="dry-run", latency_ms=5, detail={"action": "reply", "reason": "y" * 6000})
    t.emit("ticket", ticket="T2", outcome="dry-run", latency_ms=5, detail={"action": "handoff"})
    snap = TelemetryReader(tmp_path / "t.db").snapshot("r")
    assert snap["progress"]["done"] == 2 and snap["progress"]["actions"].get("handoff") == 1  # the second ticket's parse is unaffected
    assert t.stats()["dropped"] == 0


# ---- provider and model on every judge event ----------------------------------------------------------------------

def test_judge_events_record_the_provider_the_model_and_the_reported_tokens(tmp_path):
    t = Telemetry(tmp_path / "t.db", "r")
    judge = typesafe_judge(t)
    for _ in range(3):
        judge.ask("{}", {"refund_or_dispute": {"type": "noul"}})
    events = TelemetryReader(tmp_path / "t.db")
    rows = events._db().execute("SELECT stage, outcome, tokens, detail FROM events WHERE kind='model'").fetchall()
    assert len(rows) == 3 and all(r["stage"] == "judge" and r["outcome"] == "ok" and r["tokens"] == 1909 for r in rows)
    assert all(json.loads(r["detail"]) == {"provider": "TypeSafe", "model": "jev-1.14.0"} for r in rows)


def test_a_failed_call_records_the_provider_with_its_fault(tmp_path):
    t = Telemetry(tmp_path / "t.db", "r")
    bad = Judge(make_create("k", client=httpx.Client(base_url="https://api.typesafe.ai", transport=httpx.MockTransport(lambda r: httpx.Response(401)))),
                "jev-1.14.0", telemetry=t, provider="TypeSafe")
    try:
        bad.ask("{}", {"q": {"type": "noul"}})
    except Exception:
        pass
    row = sqlite3.connect(tmp_path / "t.db").execute("SELECT error_class, detail FROM events WHERE kind='fault'").fetchone()
    assert row[0] == "fatal_auth" and json.loads(row[1])["provider"] == "TypeSafe"


def test_cached_calls_are_counted_as_cached_with_the_provider(tmp_path):
    from qra.infra.cache import KVCache
    t = Telemetry(tmp_path / "t.db", "r")
    judge = typesafe_judge(t, KVCache(tmp_path / "c.db", "decisions"))
    judge.ask("{}", {"q": {"type": "noul"}})
    judge.ask("{}", {"q": {"type": "noul"}})  # served from the cache
    m = TelemetryReader(tmp_path / "t.db").snapshot("r")["models"]["judge"]
    assert (m["calls"], m["cached"], m["tokens"]) == (1, 1, 1909)


# ---- the run records its configuration, and the reader only claims a limit that exists ---------------------------

def start(path, run_id, judge):
    t = Telemetry(path, run_id)
    t.emit("run_start", detail={"workers": 2, "dry_run": True, "judge": judge, "retrieval": "exhaustive", "second_opinion": True})
    t.emit("run_start", stage="snapshot", detail={"tickets": 1})
    t.emit("model", stage="judge", outcome="ok", latency_ms=300, tokens=1909, detail=judge)
    t.emit("ticket", ticket="T1", outcome="dry-run", latency_ms=300, detail={"action": "reply"})
    t.emit("run_end", outcome="ok")


def test_a_typesafe_run_reports_its_judge_and_has_no_token_window(tmp_path):
    start(tmp_path / "t.db", "ts", {"provider": "TypeSafe", "model": "jev-1.14.0"})
    snap = TelemetryReader(tmp_path / "t.db").snapshot("ts")
    assert snap["run"]["judge"] == {"provider": "TypeSafe", "model": "jev-1.14.0"}
    assert snap["run"]["retrieval"] == "exhaustive" and snap["run"]["second_opinion"] is True
    assert snap["tokens_per_minute"]["limit"] is None  # no per-minute window to draw a limit line for
    assert snap["models"]["judge"]["tokens"] == 1909


def test_a_gemini_run_still_shows_the_fifty_thousand_token_limit(tmp_path):
    start(tmp_path / "t.db", "gm", {"provider": "Gemini", "model": "gemini-decision-1"})
    assert TelemetryReader(tmp_path / "t.db").snapshot("gm")["tokens_per_minute"]["limit"] == DECISION_TPM_LIMIT


def test_a_run_recorded_before_providers_existed_is_read_as_gemini(tmp_path):
    t = Telemetry(tmp_path / "t.db", "old")
    t.emit("run_start", detail={"workers": 2, "dry_run": True})  # the old shape: no judge field
    t.emit("ticket", ticket="T1", outcome="dry-run", latency_ms=5, detail={"action": "reply"})
    snap = TelemetryReader(tmp_path / "t.db").snapshot("old")
    assert snap["run"]["judge"] is None and snap["tokens_per_minute"]["limit"] == DECISION_TPM_LIMIT
    assert token_limit({}) == DECISION_TPM_LIMIT and token_limit({"judge": {"provider": "TypeSafe"}}) is None


def test_the_runner_writes_the_run_configuration(ledger, tmp_path):
    from test_runner import make_fake
    from test_telemetry import instrumented
    runner, reader = instrumented(make_fake(3), ledger, tmp_path, workers=2)
    runner.run()
    run = reader.snapshot("run1")["run"]
    assert run["retrieval"] == "exhaustive" and run["second_opinion"] is False  # the test runner has neither an opinion agent nor a shortlist
    assert run["judge"] is not None and set(run["judge"]) == {"provider", "model"}
