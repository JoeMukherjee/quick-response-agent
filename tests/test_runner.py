"""Runner behaviour with a fake helpdesk and fake model doubles: concurrency, resume, failure and safety paths."""
import json
import threading
import time

import pytest
from fake_helpdesk import FakeHelpdesk
from test_policy import answers

from qra.agents.drafting import Generated
from qra.core.faq import load_faq
from qra.infra.helpdesk import HelpdeskClient
from qra.decision.judge import JudgeError
from qra.core.models import Answers
from qra.infra.ratelimit import RequestLimiter
from qra.runtime.runlog import RunLog
from qra.runtime.runner import Runner, RunnerError, RunLock

FAQ = load_faq()


class FakeJudge:
    """Triage answers are chosen from the ticket subject; verification passes unless the reply says so."""

    def __init__(self, fail_for: set[str] = frozenset()):
        self.fail_for, self.asked = set(fail_for), []
        self._lock = threading.Lock()

    def ask(self, state, questions):
        subject = json.loads(state)["ticket"]["subject"]
        with self._lock:
            self.asked.append(subject)
        if subject in self.fail_for:
            raise JudgeError("model down")
        if "language" in questions:
            if subject.startswith("refund"):
                return answers(refund_or_dispute=0.9)
            return answers(faq={"F02": 0.95})
        if "covers_everything" in questions:
            return Answers(noul={"covers_everything": 0.95}, tokens=10)
        n = sum(1 for k in questions if k.startswith("u") and k[1:].isdigit())
        noul = {f"u{i}": 0.05 for i in range(n)} | {"promise": 0.02, "responsive": 0.97}
        return Answers(noul=noul, choice={"reply_language": {"nl": 0.97, "en": 0.01, "de": 0.01, "other": 0.01}}, tokens=10)


class FakeDrafter:
    def __init__(self, reply="Hoi, ga naar Team → Medewerker toevoegen en klik op Uitnodigen."):
        self.reply_text = reply
        self.replies = 0

    def reply(self, ticket, language, entries):
        self.replies += 1
        return Generated(self.reply_text, "Hi, go to Team", 5, 5, 0)

    def handoff_note(self, ticket, why, priority, category, rejected_draft=""):
        return Generated(f"Customer needs a person. Why: {why}", "", 5, 5, 0)


def make_fake(n=12, subject=lambda i: f"how to {i}"):
    return FakeHelpdesk([{"id": f"T{i:03d}", "subject": subject(i), "body": "Hoe voeg ik een medewerker toe?"}
                         for i in range(1, n + 1)])


def build(fake, ledger, tmp_path, judge=None, drafter=None, workers=4, dry_run=False):
    client = HelpdeskClient("http://helpdesk.test", "k", "me@example.com", transport=fake.transport,
                            limiter=RequestLimiter(1000), sleep=lambda s: None)
    judge = judge or FakeJudge()
    runner = Runner(client, judge, drafter or FakeDrafter(), ledger, FAQ, RunLog(tmp_path / "logs", "t"),
                    workers=workers, dry_run=dry_run)
    return runner, judge


def accepted_by_ticket(fake):
    seen = {}
    for tid, action, _ in fake.accepted_posts:
        seen[tid] = seen.get(tid, 0) + 1
    return seen


def test_many_workers_answer_every_ticket_exactly_once(ledger, tmp_path):
    fake = make_fake(40)
    runner, _ = build(fake, ledger, tmp_path, workers=8)
    summary = runner.run()
    assert summary.exit_code == 0 and summary.outcomes == {"sent": 40}
    counts = accepted_by_ticket(fake)
    assert len(counts) == 40 and set(counts.values()) == {1}


def test_a_rerun_after_a_complete_run_sends_nothing(ledger, tmp_path):
    fake = make_fake(6)
    build(fake, ledger, tmp_path)[0].run()
    again = build(make_fake(6), ledger, tmp_path)[0]
    with pytest.raises(RunnerError):  # server shows them open again but the ledger says done: refuse, do not guess
        again.run()


def test_refund_goes_to_a_human_with_a_note_and_no_draft(ledger, tmp_path):
    fake = make_fake(3, subject=lambda i: f"refund {i}")
    drafter = FakeDrafter()
    build(fake, ledger, tmp_path, drafter=drafter)[0].run()
    assert {a for _, a, _ in fake.accepted_posts} == {"handoff"} and drafter.replies == 0
    assert all("handoff_note" in body and "draft_reply" not in body for _, _, body in fake.accepted_posts)


def test_a_model_failure_leaves_that_ticket_open_and_the_run_exits_nonzero(ledger, tmp_path):
    fake = make_fake(5)
    judge = FakeJudge(fail_for={"how to 3"})
    summary = build(fake, ledger, tmp_path, judge=judge)[0].run()
    assert summary.exit_code == 1 and len(summary.errors) == 1
    assert "T003" not in accepted_by_ticket(fake) and ledger.state("T003") is None  # not handed off, not decided
    assert len(accepted_by_ticket(fake)) == 4
    judge.fail_for.clear()  # model is back: a re-run completes only the open ticket
    again = build(fake, ledger, tmp_path, judge=judge)[0].run()
    assert again.outcomes.get("sent") == 1 and set(accepted_by_ticket(fake).values()) == {1}


def test_resume_uses_the_stored_decision_and_never_asks_the_model_again(ledger, tmp_path):
    fake = make_fake(4)
    ledger.record_decided("T002", "handoff", {"language": "nl", "category": "other", "priority": "P3",
                                              "reason": "stored", "handoff_note": "stored note"})
    runner, judge = build(fake, ledger, tmp_path)
    runner.run()
    assert "how to 2" not in judge.asked
    stored = [b for t, a, b in fake.accepted_posts if t == "T002"]
    assert stored == [{"language": "nl", "category": "other", "priority": "P3", "reason": "stored",
                       "handoff_note": "stored note"}]


def test_a_reply_that_fails_verification_becomes_a_handoff_with_the_draft_kept_unsent(ledger, tmp_path):
    fake = make_fake(2)
    drafter = FakeDrafter(reply="Plus kost EUR 99 per medewerker, probeer het gratis.")
    build(fake, ledger, tmp_path, drafter=drafter)[0].run()
    assert {a for _, a, _ in fake.accepted_posts} == {"handoff"}
    assert not any("draft_reply" in b for _, _, b in fake.accepted_posts)


def test_dry_run_posts_nothing_and_writes_no_ledger_entries(ledger, tmp_path):
    fake = make_fake(5)
    summary = build(fake, ledger, tmp_path, dry_run=True)[0].run()
    assert fake.accepted_posts == [] and summary.outcomes == {"dry-run": 5} and ledger.counts() == {}


def test_logs_have_one_line_per_ticket_and_never_the_ticket_text(ledger, tmp_path):
    fake = make_fake(7)
    build(fake, ledger, tmp_path)[0].run()
    lines = [json.loads(l) for l in (tmp_path / "logs" / "run-t.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 7 and {l["ticket_id"] for l in lines} == {f"T{i:03d}" for i in range(1, 8)}
    assert all("Hoe voeg ik" not in json.dumps(l) for l in lines)


def test_the_run_lock_admits_one_runner_at_a_time(tmp_path):
    with RunLock(tmp_path / "run.lock"):
        with pytest.raises(RunnerError):
            with RunLock(tmp_path / "run.lock"):
                pass
    with RunLock(tmp_path / "run.lock"):  # released on exit
        pass


def test_threads_never_exceed_the_request_rate():
    limiter = RequestLimiter(20)  # 20 per second, real clock
    stamps, lock = [], threading.Lock()

    def worker():
        for _ in range(8):
            limiter.wait()
            with lock:
                stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    stamps.sort()
    assert len(stamps) == 40
    worst = max(sum(1 for s in stamps if t0 <= s < t0 + 1.0) for t0 in stamps)
    assert worst <= 21, f"{worst} requests in one second"


def test_log_scrub_keeps_json_valid_and_does_not_mistake_long_floats_for_cards():
    from qra.runtime.runlog import scrub
    out = scrub({"triggers": {"refund": 0.9841234567890123456}, "note": "IBAN NL91 ABNA 0417 1643 00", "body": "secret"})
    assert out["triggers"]["refund"] == 0.9841 and "NL91" not in out["note"] and "body" not in out
    json.dumps(out)


def test_an_invalid_key_aborts_the_run_cleanly_and_hands_nothing_off(ledger, tmp_path):
    from qra.core.errors import FatalAuthError

    class BadKeyJudge(FakeJudge):
        def ask(self, state, questions):
            raise FatalAuthError("The Gemini API key is invalid or not authorised")

    fake = make_fake(10)
    runner, _ = build(fake, ledger, tmp_path, judge=BadKeyJudge())
    with pytest.raises(FatalAuthError):
        runner.run()
    assert fake.accepted_posts == [] and ledger.counts() == {}  # nothing handed off, nothing decided


def test_helpdesk_down_while_listing_is_reported_not_raised(ledger, tmp_path):
    fake = make_fake(3)
    fake.inject("GET /tickets", *["503"] * 20)
    summary = build(fake, ledger, tmp_path)[0].run()
    assert summary.exit_code == 1 and "could not list open tickets" in summary.errors[0]


def test_judge_classifies_failures_three_ways():
    from qra.core.errors import FatalAuthError
    from qra.decision.judge import Judge, JudgeError, PermanentJudgeError

    class Boom(Exception):
        def __init__(self, code, msg):
            super().__init__(msg)
            self.code = code

    def judge_for(exc):
        def create(**_):
            raise exc
        return Judge(create, "m", sleep=lambda s: None)

    with pytest.raises(FatalAuthError):
        judge_for(Boom(400, "API key not valid. Please pass a valid API key.")).ask("{}", {})
    with pytest.raises(FatalAuthError):
        judge_for(Boom(403, "forbidden")).ask("{}", {})
    with pytest.raises(PermanentJudgeError):
        judge_for(Boom(400, "Request blocked (safety)")).ask("{}", {})
    with pytest.raises(JudgeError) as e:  # our request was wrong: visible error, ticket stays open, no handoff
        judge_for(Boom(400, "Invalid argument")).ask("{}", {})
    assert not isinstance(e.value, PermanentJudgeError)
    calls = []

    def flaky(**_):
        calls.append(1)
        raise Boom(503, "unavailable")
    with pytest.raises(JudgeError):
        Judge(flaky, "m", sleep=lambda s: None).ask("{}", {})
    assert len(calls) == Judge.MAX_TRIES  # transient: retried, then an error (never an answer)


def test_cost_per_100_tickets_is_computed_from_logged_tokens(tmp_path):
    from qra.evaluation.cost import summarize
    log = tmp_path / "run.jsonl"
    rows = [{"ticket_id": f"T{i}", "model_calls": {"judge_in": 4000, "verify_in": 1000, "draft_in": 800,
                                                  "draft_out": 100, "draft_thought": 100, "note_in": 0, "note_out": 0}}
            for i in range(10)]
    log.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    out = summarize(log)
    # per ticket: decision 5000 x 0.10/1M = 0.0005 ; flash in 800 x 0.75/1M = 0.0006 ; out 200 x 3.75/1M = 0.00075
    assert out["tickets"] == 10 and abs(out["usd_per_100_tickets"]["total"] - 0.185) < 1e-6


def test_a_dry_run_ignores_a_stale_ledger_and_still_processes_every_ticket(ledger, tmp_path):
    fake = make_fake(5)
    for i in range(1, 6):  # the ledger says everything was already sent (e.g. from an earlier run before a reset)
        ledger.record_decided(f"T{i:03d}", "reply", {"x": 1})
        ledger.record_sent(f"T{i:03d}")
    judge = FakeJudge()
    summary = build(fake, ledger, tmp_path, judge=judge, dry_run=True)[0].run()
    assert summary.outcomes == {"dry-run": 5} and len(judge.asked) >= 5 and fake.accepted_posts == []
