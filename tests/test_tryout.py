"""Try a ticket: input parsing, the read-only decision, and the two endpoints. No network, no model."""
import json
import threading
import time

import pytest

from qra.core.config import Settings
from qra.core.models import Answers
from qra.runtime import tryout
from qra.runtime.tryout import TryError, parse_ticket, run_try
from test_policy import answers
from test_runner import FakeDrafter
from test_server import call, srv  # noqa: F401  (the fixture and the request helper)


# ---- parsing: fields or pasted JSON --------------------------------------------------------------------------------

def test_fields_and_pasted_json_give_the_same_ticket():
    a = parse_ticket({"subject": "Factuur", "body": "Waar vind ik mijn factuur?", "customer_plan": "Plus", "channel": "email"})
    b = parse_ticket('{"subject": "Factuur", "body": "Waar vind ik mijn factuur?", "customer_plan": "Plus", "channel": "email"}')
    assert a == b and a.body == "Waar vind ik mijn factuur?" and a.customer_plan == "Plus"


def test_the_helpdesks_own_shape_and_common_aliases_are_accepted():
    t = parse_ticket({"ticket": {"id": "T9", "subject": "Hi", "message": "text here", "plan": "Basis"}})
    assert (t.subject, t.body, t.customer_plan) == ("Hi", "text here", "Basis")
    assert t.id == "TRY"  # a typed ticket never borrows a real id


@pytest.mark.parametrize("raw, fragment", [
    ("{not json", "not valid JSON"),
    ("[1, 2]", "JSON object"),
    ({}, "subject or a body"),
    ({"subject": 5}, "must be text"),
    ({"body": "x" * 8001}, "limit is 8000"),
    ({"subject": "x" * 301, "body": "ok"}, "limit is 300"),
])
def test_bad_input_is_a_message_not_a_crash(raw, fragment):
    with pytest.raises(TryError, match=fragment):
        parse_ticket(raw)


# ---- the decision ---------------------------------------------------------------------------------------------------

class FakeJudge:
    model, provider, calls, tokens, _cache = "gemini-decision-1", "Gemini", 0, 0, object()

    def ask(self, state, questions):
        if "language" in questions:
            return answers(faq={"F02": 0.95})
        if "covers_everything" in questions:
            return Answers(noul={"covers_everything": 0.95}, tokens=10)
        noul = {k: 0.05 for k in questions if k.startswith("u")} | {"promise": 0.02, "responsive": 0.97}
        return Answers(noul=noul, choice={"reply_language": {"nl": 0.97, "en": 0.01, "de": 0.01, "other": 0.01}}, tokens=10)


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(tryout.service, "make_judge", lambda s, tel=None: FakeJudge())
    monkeypatch.setattr("qra.agents.drafting.Drafter", lambda *a, **k: FakeDrafter())
    return Settings(helpdesk_url="https://h.test", helpdesk_key="k", typesafe_api_key="t")


def test_a_typed_ticket_gets_a_full_decision_and_nothing_is_posted(patched):
    out = run_try(patched, parse_ticket({"subject": "Medewerker", "body": "Hoe voeg ik een medewerker toe?"}), second_opinion=False)
    assert out["posted"] is False and out["decision"]["action"] == "reply" and out["decision"]["faq_ids"] == ["F02"]
    assert out["reply"] and not out["handoff_note"] and out["verification"] == {"ran": True, "failures": []}
    assert out["judgments"]["faq"][0][0] == "F02" and out["cost"]["judge_calls"] >= 2 and "decide" in out["seconds"]


def test_an_unusable_choice_is_refused_before_any_model_call(patched):
    t = parse_ticket({"subject": "x"})
    with pytest.raises(TryError, match="judge must be"):
        run_try(patched, t, judge="groq")
    with pytest.raises(TryError, match="retrieval must be"):
        run_try(patched, t, retrieval="graph")
    with pytest.raises(TryError, match="TYPESAFE_API_KEY"):
        from dataclasses import replace
        run_try(replace(patched, typesafe_api_key=""), t, judge="typesafe")


def test_at_most_two_tickets_are_decided_at_once(patched):
    held = [tryout._BUSY.acquire(blocking=False) for _ in range(2)]
    try:
        with pytest.raises(TryError, match="already being decided"):
            run_try(patched, parse_ticket({"subject": "x"}))
    finally:
        for h in held:
            if h:
                tryout._BUSY.release()


# ---- the endpoints --------------------------------------------------------------------------------------------------

def test_try_needs_the_cookie_and_the_custom_header(srv):
    body = {"ticket": {"subject": "x", "body": "y"}}
    assert call(srv, "/api/try", method="POST", cookie=False, body=body)[0] == 403
    assert call(srv, "/api/try", method="POST", body=body, headers={})[0] == 403


def test_try_reports_bad_input_as_a_400_with_a_message(srv):
    code, raw = call(srv, "/api/try", method="POST", body={"json": "{oops"})
    assert code == 400 and "not valid JSON" in json.loads(raw)["error"]
    code, raw = call(srv, "/api/try", method="POST", body={"ticket": {}})
    assert code == 400 and "subject or a body" in json.loads(raw)["error"]
    assert call(srv, "/api/try", method="POST", body={"ticket": {"subject": "x"}, "judge": "groq"})[0] == 400


def test_a_ticket_may_be_long_but_not_enormous(srv):
    long_ok = {"ticket": {"subject": "x", "body": "y" * 7000}, "judge": "groq"}  # past the 4 KB limit of other posts, then refused on the judge
    assert call(srv, "/api/try", method="POST", body=long_ok)[0] == 400
    assert call(srv, "/api/try", method="POST", body={"json": "z" * 40000})[0] == 413
    assert call(srv, "/api/cache", method="POST", body={"namespace": "x" * 5000})[0] in (400, 413)  # other routes keep the small cap


def test_the_case_list_works_offline_with_the_written_cases(srv, monkeypatch):
    def boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(tryout.service, "make_client", boom)
    code, raw = call(srv, "/api/cases")
    data = json.loads(raw)
    ids = [g["id"] for g in data["groups"]]
    assert code == 200 and ids == ["authored", "heldout"] and data["notes"] and len(data["groups"][0]["cases"]) >= 20
    assert all("subject" in c and "body" in c for g in data["groups"] for c in g["cases"])


def test_the_bench_eval_names_its_test_set_and_rejects_an_unknown_one(srv):
    assert call(srv, "/api/job", method="POST", body={"kind": "eval", "suite": "nordavo"})[0] == 200
    import time as _t
    _t.sleep(0.3)
    assert ("eval", {"suite": "nordavo"}) in srv.stub.calls
    code, raw = call(srv, "/api/job", method="POST", body={"kind": "eval", "suite": "everything"})
    assert code == 400 and "suite must be" in json.loads(raw)["error"]


def test_nordavos_examples_are_fetched_live_on_every_eval(monkeypatch, tmp_path):
    """The memo that spares repeated calls elsewhere must never answer for a Bench test: each eval asks the API again."""
    from qra.runtime import service
    seen = []

    class Client:
        def examples(self, max_age=600.0):
            seen.append(max_age)
            raise RuntimeError("stop after the fetch")

    monkeypatch.setattr(service, "make_client", lambda s, tel=None: Client())
    monkeypatch.setattr(service, "make_judge", lambda s, tel=None: object())
    monkeypatch.setattr(service, "make_opinion", lambda *a, **k: None)
    service.execute_eval(Settings(helpdesk_url="https://h.test", helpdesk_key="k", state_dir=tmp_path / "s", log_dir=tmp_path / "l"), suite="nordavo")
    assert seen == [0]


def test_the_ticket_api_can_be_changed_but_the_saved_key_never_follows_it(srv):
    from qra.server.jobs import parse_api
    s = srv.settings
    assert parse_api(s, None) is None and parse_api(s, {"url": "", "key": ""}) is None
    assert parse_api(s, {"url": "https://other.example.com/api", "key": "k2"}) == ("https://other.example.com/api", "k2")
    assert parse_api(s, {"url": s.helpdesk_url}) == (s.helpdesk_url.rstrip("/"), s.helpdesk_key)  # same address: the saved key is fine
    for bad, why in (({"url": "https://other.example.com"}, "saved key"),          # another address with no key of its own
                     ({"url": "http://other.example.com", "key": "k"}, "https"),    # plain http to a remote host
                     ({"url": "ftp://x", "key": "k"}, "https"), ("nope", "object")):
        with pytest.raises(ValueError, match=why):
            parse_api(s, bad)
    assert parse_api(s, {"url": "http://localhost:9000", "key": "k"}) == ("http://localhost:9000", "k")


def test_a_job_started_with_another_api_reports_the_address_and_never_the_key(srv):
    gate, orig, seen = threading.Event(), srv.stub.execute_eval, {}

    def hold(s, out=None, **kw):
        seen["url"], seen["key"] = s.helpdesk_url, s.helpdesk_key  # what the job itself is given
        gate.wait(3)
        return orig(s, **kw)
    srv.stub.execute_eval = hold
    code, _ = call(srv, "/api/job", method="POST", body={"kind": "eval", "suite": "ours", "api": {"url": "https://other.example.com", "key": "SECRET-KEY-123"}})
    assert code == 200
    status = call(srv, "/api/job")[1].decode()
    assert "other.example.com" in status and "SECRET-KEY-123" not in status      # the dashboard never echoes the key
    time.sleep(0.3)
    assert seen == {"url": "https://other.example.com", "key": "SECRET-KEY-123"}  # but the job does use it
    gate.set()
    assert call(srv, "/api/job", method="POST", body={"kind": "eval", "api": {"url": "https://other.example.com"}})[0] == 400
