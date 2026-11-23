"""TypeSafe (Jev) as the judge: same questions and rules, different transport. No network: HTTP is mocked."""
import json

import httpx
import pytest

from qra.core.config import ConfigError, Settings
from qra.core.errors import FatalAuthError
from qra.decision.judge import Judge, JudgeError
from qra.decision.questions import ticket_state, triage_questions
from qra.decision.typesafe import DEFAULT_MODEL, TypeSafeHTTPError, make_create
from test_policy import FAQ
from test_resample import TICKET

REPLY = {"model": "jev-1.14.0", "usage": {"input_tokens": 1909, "output_tokens": 526},
         "answers": {"language": {"type": "choice", "choice": "nl", "confidence": 0.99, "probabilities": {"nl": 0.99, "en": 0.01}},
                     "refund_or_dispute": {"type": "noul", "noul": 0.98}}}


def client(handler):
    return httpx.Client(base_url="https://api.typesafe.ai", transport=httpx.MockTransport(handler))


def test_the_request_has_the_documented_shape_and_the_key_goes_only_in_the_header():
    seen = {}

    def handler(request):
        seen.update(headers=dict(request.headers), url=str(request.url), body=json.loads(request.content))
        return httpx.Response(200, json=REPLY)

    create = make_create("secret-key", client=client(handler))
    create(model=DEFAULT_MODEL, input=ticket_state(TICKET), questions={"refund_or_dispute": {"type": "noul", "instructions": "x"}})
    assert seen["url"] == "https://api.typesafe.ai/v1/systemone"
    assert seen["headers"]["authorization"] == "Bearer secret-key"
    assert "secret-key" not in json.dumps(seen["body"])
    assert seen["body"]["model"] == "jev-1.14.0"  # pinned to a version, not the moving alias
    assert isinstance(seen["body"]["state"], dict) and seen["body"]["state"]["ticket"]["message"] == TICKET.body  # named fields


def test_the_answer_is_parsed_into_the_same_answers_the_gemini_judge_produces():
    judge = Judge(make_create("k", client=client(lambda r: httpx.Response(200, json=REPLY))), DEFAULT_MODEL, provider="TypeSafe")
    a = judge.ask(ticket_state(TICKET), {"language": {"type": "choice"}, "refund_or_dispute": {"type": "noul"}})
    assert a.p("refund_or_dispute") == 0.98 and a.top("language")[0] == "nl" and a.tokens == 1909
    assert judge.calls == 1 and judge.tokens == 1909


def test_a_429_waits_for_retry_after_and_then_succeeds():
    calls, waits = [], []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "2"}) if len(calls) < 3 else httpx.Response(200, json=REPLY)

    create = make_create("k", client=client(handler), sleep=waits.append)
    create(model=DEFAULT_MODEL, input="{}", questions={"q": {"type": "noul"}})
    assert len(calls) == 3 and waits == [2.0, 2.0]


def test_a_rate_limit_that_never_clears_is_reported_not_swallowed():
    create = make_create("k", client=client(lambda r: httpx.Response(429, headers={"retry-after": "0"})), sleep=lambda s: None)
    with pytest.raises(TypeSafeHTTPError) as e:
        create(model=DEFAULT_MODEL, input="{}", questions={"q": {"type": "noul"}})
    assert e.value.status_code == 429


@pytest.mark.parametrize("status", [401, 403])
def test_an_invalid_key_is_the_fatal_case_and_names_the_right_provider(status):
    judge = Judge(make_create("bad", client=client(lambda r: httpx.Response(status, json={"error": "unauthorized"}))),
                  DEFAULT_MODEL, provider="TypeSafe")
    with pytest.raises(FatalAuthError, match="TypeSafe API key"):
        judge.ask(ticket_state(TICKET), {"q": {"type": "noul"}})


def test_a_5xx_is_retried_and_other_4xx_are_rejected_loudly():
    attempts = []

    def flaky(request):
        attempts.append(1)
        return httpx.Response(503) if len(attempts) < 3 else httpx.Response(200, json=REPLY)

    judge = Judge(make_create("k", client=client(flaky)), DEFAULT_MODEL, sleep=lambda s: None, provider="TypeSafe")
    assert judge.ask(ticket_state(TICKET), {"q": {"type": "noul"}}).p("refund_or_dispute") == 0.98 and len(attempts) == 3
    bad = Judge(make_create("k", client=client(lambda r: httpx.Response(400, json={"error": "bad question"}))), DEFAULT_MODEL, provider="TypeSafe")
    with pytest.raises(JudgeError, match="rejected"):
        bad.ask(ticket_state(TICKET), {"q": {"type": "noul"}})


def test_the_client_refuses_plain_http_and_never_follows_a_redirect():
    with pytest.raises(ValueError):
        make_create("k", base_url="http://api.typesafe.ai")
    create = make_create("k", client=client(lambda r: httpx.Response(302, headers={"location": "https://evil.example/"})))
    with pytest.raises(TypeSafeHTTPError):
        create(model=DEFAULT_MODEL, input="{}", questions={"q": {"type": "noul"}})


def test_the_full_question_set_is_accepted_as_is():
    assert len(triage_questions(FAQ)) >= 20  # the very questions sent to Gemini go to TypeSafe unchanged


def test_the_backend_is_chosen_by_setting_and_needs_its_key(tmp_path):
    from dataclasses import replace
    from qra.runtime.service import make_judge
    base = Settings(helpdesk_url="https://h.test", helpdesk_key="k", state_dir=tmp_path / "state", log_dir=tmp_path / "logs", judge_backend="gemini")
    with pytest.raises(ConfigError, match="TYPESAFE_API_KEY"):
        make_judge(replace(base, judge_backend="typesafe"))
    judge = make_judge(replace(base, judge_backend="typesafe", typesafe_api_key="k"))
    assert judge.provider == "TypeSafe" and judge.model == "jev-1.14.0"
    assert base.judge_backend == "gemini" and base.model_judge == "gemini-decision-1"  # the default is untouched


def test_cached_answers_are_kept_per_model_so_the_two_backends_never_share_them(tmp_path):
    from qra.infra.cache import KVCache
    cache = KVCache(tmp_path / "c.db", "decisions")
    a = Judge(lambda **k: None, "gemini-decision-1", cache)
    b = Judge(lambda **k: None, "jev-1.14.0", cache)
    assert a._key("s", {"q": 1}) != b._key("s", {"q": 1})


# ---- the two modes differ only in the decision model ----------------------------------------------------------------

def test_nothing_in_the_decision_pipeline_branches_on_the_backend():
    """The backend is chosen in exactly one place (make_judge) and shown in telemetry. If the rule table, the questions, the
    pipeline, the runner, the drafter, the opinion agent or the verification ever mention it, the two modes would differ in more
    than the decision model, and a comparison between them would mean nothing."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "qra"
    decision_path = ["decision/policy.py", "decision/questions.py", "runtime/pipeline.py", "runtime/runner.py", "agents/drafting.py",
                     "agents/opinion.py", "safety/verify.py", "safety/checks.py", "safety/redact.py", "core/models.py"]
    import re
    branching = re.compile(r"""judge_backend|==\s*["']typesafe|==\s*["']gemini|provider\s*[!=]=|\.provider\s*[!=]=|if[^
]*typesafe""", re.I)
    for rel in decision_path:
        text = (root / rel).read_text(encoding="utf-8")
        assert not branching.search(text), f"{rel} branches on the backend"  # mentions in a docstring or a telemetry label are fine


def test_two_settings_that_differ_only_in_the_backend_ask_the_judge_identically(tmp_path):
    from dataclasses import replace
    from test_resample import ScriptedJudge, TICKET
    from qra.runtime.pipeline import plan_ticket
    from test_policy import answers
    base = Settings(helpdesk_url="https://h.test", helpdesk_key="k", state_dir=tmp_path / "s", log_dir=tmp_path / "l", typesafe_api_key="k", judge_backend="gemini")
    ts = replace(base, judge_backend="typesafe")
    from dataclasses import fields
    assert [f.name for f in fields(base) if getattr(base, f.name) != getattr(ts, f.name)] == ["judge_backend"]  # nothing else differs
    seen = {}
    for name in ("gemini", "typesafe"):
        judge = ScriptedJudge([answers(faq={"F02": 0.97})] * 2)
        plan_ticket(TICKET, judge, FAQ)  # the pipeline is handed a judge and cannot tell which provider is behind it
        seen[name] = judge.requests
    assert seen["gemini"] == seen["typesafe"]


def test_both_backends_receive_exactly_the_same_input_and_questions():
    """What differs between the two modes on the wire is the model name and nothing else: the state text and every question
    handed to the Gemini call arrive at TypeSafe unchanged (as the same JSON, parsed into named fields)."""
    from qra.safety.verify import _state, verify_questions
    from qra.decision.questions import coverage_questions, coverage_state
    entries = [FAQ["F02"]]
    reply = "Hoi, ga naar Team en klik op Uitnodigen. Groet."
    sent = [(ticket_state(TICKET), triage_questions(FAQ)),
            (ticket_state(TICKET, sample=1), triage_questions(FAQ)),
            (coverage_state(TICKET, entries), coverage_questions()),
            (_state(TICKET, entries, reply), verify_questions(reply))]
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=REPLY)

    create = make_create("k", client=client(handler))
    gemini_side = []
    for state, questions in sent:
        gemini_side.append((state, questions))                       # what the Gemini call is given: model, input=state, questions
        create(model=DEFAULT_MODEL, input=state, questions=questions)  # what the TypeSafe call is given: the same arguments
    for (state, questions), body in zip(gemini_side, seen):
        assert json.dumps(body["state"], ensure_ascii=False) == state  # the same text, down to the key order
        assert body["questions"] == questions
        assert set(body) == {"model", "state", "questions"}  # nothing extra is sent to either service


def test_jev_is_the_default_decision_model_when_it_can_be_used(monkeypatch):
    """Recommended default: Jev whenever a TypeSafe key is set; Decision 1 when it is not (so a Gemini-only machine still runs);
    JUDGE_BACKEND always wins."""
    from qra.core.config import _default_backend
    for var in ("JUDGE_BACKEND", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert _default_backend() == "gemini"                      # no key: fall back so the service still runs
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    assert _default_backend() == "typesafe"                    # key present: the recommended model
    monkeypatch.setenv("JUDGE_BACKEND", "gemini")
    assert _default_backend() == "gemini"                      # an explicit choice is respected
    monkeypatch.setenv("JUDGE_BACKEND", "nonsense")
    assert _default_backend() == "typesafe"                    # an unknown value is ignored
    assert Settings(helpdesk_url="https://h", helpdesk_key="k").model_draft == "gemini-3.5-flash-lite"
    assert Settings(helpdesk_url="https://h", helpdesk_key="k").model_opinion == "gemini-3.8-flash"
