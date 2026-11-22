"""Response caching: identical requests are free, any change to what determines an answer invalidates it."""
from types import SimpleNamespace

from qra.agents import drafting
from qra.agents.drafting import Draft, Drafter, Note
from qra.core.faq import load_faq
from qra.core.models import Ticket
from qra.decision.questions import triage_questions
from qra.infra.cache import KVCache, cache_key

FAQ = load_faq()
TICKET = Ticket("X1", "Hoe voeg ik toe", "Hoe voeg ik een medewerker toe?", "email", "Basis")


class FakeAgent:
    """Stands in for an Agno agent and counts how many real model calls happen."""
    calls = 0

    def __init__(self, schema):
        self.schema = schema

    def run(self, prompt):
        FakeAgent.calls += 1
        content = (Draft(reply="Hoi, ga naar Team.", english_gloss="Hi, go to Team.") if self.schema is Draft
                   else Note(summary="s", why_escalated="w", next_step="n", facts=["f"]))
        return SimpleNamespace(content=content, metrics=SimpleNamespace(input_tokens=50, output_tokens=20, reasoning_tokens=5))


def make_drafter(tmp_path, monkeypatch):
    FakeAgent.calls = 0
    d = Drafter("m", "k", sleep=lambda s: None, cache=KVCache(tmp_path / "c.db", "drafts"))
    monkeypatch.setattr(d, "_agent", lambda instructions, schema: FakeAgent(schema))
    return d


def test_an_identical_draft_request_is_served_from_the_cache_at_zero_tokens(tmp_path, monkeypatch):
    d = make_drafter(tmp_path, monkeypatch)
    first = d.reply(TICKET, "nl", [FAQ["F02"]])
    second = d.reply(TICKET, "nl", [FAQ["F02"]])
    assert FakeAgent.calls == 1 and first.text == second.text
    assert first.in_tokens == 50 and second.in_tokens == 0 and second.out_tokens == 0  # a hit costs nothing


def test_a_different_ticket_entry_language_or_instruction_is_a_miss(tmp_path, monkeypatch):
    d = make_drafter(tmp_path, monkeypatch)
    d.reply(TICKET, "nl", [FAQ["F02"]])
    d.reply(Ticket("X1", "Hoe voeg ik toe", "Een andere vraag", "email", "Basis"), "nl", [FAQ["F02"]])
    d.reply(TICKET, "nl", [FAQ["F03"]])
    d.reply(TICKET, "en", [FAQ["F02"]])
    assert FakeAgent.calls == 4
    monkeypatch.setattr(drafting, "REPLY_INSTRUCTIONS", drafting.REPLY_INSTRUCTIONS + " Be extra brief.")
    d.reply(TICKET, "nl", [FAQ["F02"]])  # reworded instructions must never serve the old answer
    assert FakeAgent.calls == 5


def test_handoff_notes_are_cached_too(tmp_path, monkeypatch):
    d = make_drafter(tmp_path, monkeypatch)
    d.handoff_note(TICKET, "why", "P2", "billing")
    d.handoff_note(TICKET, "why", "P2", "billing")
    assert FakeAgent.calls == 1


def test_cache_survives_a_restart_and_reports_its_hit_rate(tmp_path):
    path = tmp_path / "c.db"
    c = KVCache(path, "drafts")
    c.put(cache_key("a", {"x": 1}), {"v": 1})
    again = KVCache(path, "drafts")
    assert again.get(cache_key("a", {"x": 1})) == {"v": 1} and again.get("missing") is None
    s = again.stats()
    assert (s["namespace"], s["hits"], s["misses"], s["hit_rate"], s["entries"]) == ("drafts", 1, 1, 0.5, 1)


def test_namespaces_do_not_collide(tmp_path):
    path = tmp_path / "c.db"
    KVCache(path, "decisions").put("k", {"v": "decision"})
    assert KVCache(path, "drafts").get("k") is None


def test_the_question_set_is_built_once_per_help_centre_version():
    assert triage_questions(FAQ) is triage_questions(load_faq())  # same digest -> the same memoised object


def test_examples_are_memoised_but_tickets_never_are(client, fake):
    fake.examples = None  # the fake has no /examples route, so serve one through a handler wrapper
    import httpx
    hits = []

    def handler(req):
        hits.append(req.url.path)
        if req.url.path == "/examples":
            return httpx.Response(200, json={"examples": [{"id": "E1"}]})
        return httpx.Response(200, json={"id": "T", "handled": None})

    from qra.infra.helpdesk import HelpdeskClient
    from qra.infra.ratelimit import RequestLimiter
    c = HelpdeskClient("http://helpdesk.test", "k", "me@example.com", transport=httpx.MockTransport(handler),
                       limiter=RequestLimiter(1000))
    c.examples()
    c.examples()
    c.get_ticket("T")
    c.get_ticket("T")
    assert hits.count("/examples") == 1 and hits.count("/tickets/T") == 2


def test_an_empty_decision_cache_still_stores_and_serves_responses(tmp_path):
    """Regression: an empty cache is falsy (it defines __len__), so `if cache:` silently skipped every write."""
    from types import SimpleNamespace
    from qra.decision.judge import Judge

    calls = []

    def create(**kw):
        calls.append(1)
        answers = {"q": SimpleNamespace(type="noul", noul=0.9)}
        return SimpleNamespace(answers=answers, usage=SimpleNamespace(total_input_tokens=100))

    judge = Judge(create, "m", cache=KVCache(tmp_path / "c.db", "decisions"))
    first = judge.ask("{}", {"q": {"type": "noul", "instructions": "x"}})
    second = judge.ask("{}", {"q": {"type": "noul", "instructions": "x"}})
    assert len(calls) == 1 and first.noul == second.noul
    assert judge.cache_hits == 1 and judge.cache_stats()["entries"] == 1
