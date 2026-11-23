"""Try one ticket: the whole decision (judge, rule table, coverage, second opinion, draft or note, verification) with either
decision model, and nothing is posted anywhere. Used by the dashboard's "Try a ticket" panel.

The ticket can be a stored test case (Nordavo's examples, the cases written for the eval, the practice tickets, read-only)
or anything typed or pasted. Input is validated and bounded here, because the text is untrusted and goes to a model.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from typing import Any

from qra.core.config import Settings
from qra.core.faq import load_faq
from qra.core.models import Ticket
from qra.decision.policy import explain
from qra.evaluation.evaluate import authored_cases, nordavo_cases
from qra.runtime import service
from qra.runtime.pipeline import plan_ticket
from qra.runtime.runner import Runner, _judgments

LIMITS = {"subject": 300, "body": 8000, "customer_plan": 40, "channel": 40}
MAX_BODY_BYTES = 32768  # for a POST that carries a ticket
_BUSY = threading.BoundedSemaphore(2)  # each try makes several paid model calls: never more than two at once


class TryError(ValueError):
    """The input is not a usable ticket (a message for the person, never a stack trace)."""


def parse_ticket(raw: Any) -> Ticket:
    """A ticket from filled-in fields or from a pasted JSON object. Accepts the helpdesk's own field names and a few
    obvious aliases (`text`, `message`, `plan`); unknown fields are ignored; every field is bounded."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TryError(f"that is not valid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})") from exc
    if isinstance(raw, dict) and isinstance(raw.get("ticket"), dict):
        raw = raw["ticket"]  # tolerate {"ticket": {...}} as the API's detail view returns it
    if not isinstance(raw, dict):
        raise TryError("a ticket is a JSON object with at least a subject or a body")

    def text(*names: str) -> str:
        for n in names:
            v = raw.get(n)
            if v is None:
                continue
            if not isinstance(v, str):
                raise TryError(f"{n} must be text")
            return v.strip()
        return ""

    subject, body = text("subject", "title"), text("body", "message", "text")
    plan, channel = text("customer_plan", "plan"), text("channel")
    if not subject and not body:
        raise TryError("give the ticket a subject or a body (an empty ticket is closed without any model call)")
    for name, value in (("subject", subject), ("body", body), ("customer_plan", plan), ("channel", channel)):
        if len(value) > LIMITS[name]:
            raise TryError(f"{name} is {len(value)} characters; the limit is {LIMITS[name]}")
    return Ticket("TRY", subject, body, channel, plan)


def _as_row(t: Ticket, label: dict | None = None, **extra) -> dict:
    row = {"id": t.id, "subject": t.subject, "body": t.body, "channel": t.channel, "customer_plan": t.customer_plan}
    if label:
        row["label"] = label
    return row | extra


class CaseCatalog:
    """The stored test cases for the picker. Nordavo's examples and the practice tickets are read-only GETs, memoised for ten
    minutes; the written cases come from eval/*.jsonl."""

    def __init__(self, settings: Settings, ttl: float = 600.0):
        self.s, self.ttl = settings, ttl
        self._lock = threading.Lock()
        self._at = 0.0
        self._value: dict | None = None

    def get(self) -> dict:
        with self._lock:
            if self._value is not None and time.monotonic() - self._at < self.ttl:
                return self._value
            groups = [
                {"id": "authored", "title": "Written for the eval", "cases": [_as_row(c.ticket, c.label) for c in authored_cases()]},
            ]
            notes: list[str] = []
            try:
                client = service.make_client(self.s)
                groups.insert(0, {"id": "nordavo", "title": "Nordavo's labelled examples",
                                  "cases": [_as_row(c.ticket, c.label) for c in nordavo_cases(client.examples())]})
                groups.insert(1, {"id": "practice", "title": "Practice tickets (no labels)",
                                  "cases": [_as_row(Ticket.from_api(t)) for t in client.all_tickets()]})
            except Exception as exc:  # offline or a bad key: the written cases and the custom box still work
                notes.append(f"Nordavo's helpdesk is not reachable ({type(exc).__name__}); only the written cases are listed")
            self._value = {"groups": groups, "notes": notes}
            self._at = time.monotonic()
            return self._value


def run_try(settings: Settings, ticket: Ticket, *, judge: str = "gemini", retrieval: str = "exhaustive",
            second_opinion: bool | None = None) -> dict:
    """Decide and draft one ticket. Always live (no cache), so the latency and token counts are the real ones."""
    if judge not in ("gemini", "typesafe"):
        raise TryError("judge must be gemini or typesafe")
    if retrieval not in ("exhaustive", "cascade"):
        raise TryError("retrieval must be exhaustive or cascade")
    if judge == "typesafe" and not settings.typesafe_api_key:
        raise TryError("TYPESAFE_API_KEY is not set in .env")
    if not _BUSY.acquire(blocking=False):
        raise TryError("two tickets are already being decided; try again in a few seconds")
    try:
        s = replace(settings, judge_backend=judge, retrieval=retrieval,
                    second_opinion=settings.second_opinion if second_opinion is None else bool(second_opinion))
        from qra.agents.drafting import Drafter
        faq = load_faq()
        j = service.make_judge(replace(s, state_dir=s.state_dir), None)
        j._cache = None  # a try is always a live measurement
        drafter = Drafter(s.model_draft, s.gemini_api_key)
        opinion = service.make_opinion(s, faq, None)
        shortlister = service.make_shortlister(s, faq)
        runner = Runner(None, j, drafter, None, faq, None)
        t0 = time.perf_counter()
        planned = plan_ticket(ticket, j, faq, opinion=opinion, shortlister=shortlister)
        t_plan = time.perf_counter() - t0
        calls: dict = {}
        action, payload, gloss, failures = runner._build(ticket, planned.plan, calls)
        total = time.perf_counter() - t0
        p = planned.plan
        judgments = _judgments(planned) or {}
        faq_scores = sorted(((k[4:], v) for k, v in judgments.items() if k.startswith("faq_") and isinstance(v, float)),
                            key=lambda kv: -kv[1])[:5]
        yes_no = {k: v for k, v in judgments.items() if isinstance(v, float) and not k.startswith("faq_")}
        choices = {k: v for k, v in judgments.items() if isinstance(v, dict)}
        return {
            "model": {"judge": j.model, "provider": j.provider, "draft": s.model_draft, "retrieval": retrieval,
                      "second_opinion": bool(opinion)},
            "decision": {"action": action.value, "category": payload["category"], "priority": payload["priority"],
                         "language": payload["language"], "reason": payload["reason"], "confidence": round(float(p.confidence), 3),
                         "row": p.row, "faq_ids": list(p.faq_ids), "triggers": {k: round(v, 3) for k, v in p.triggers.items()}},
            "reply": payload.get("draft_reply", ""), "handoff_note": payload.get("handoff_note", ""), "english_gloss": gloss,
            "verification": {"ran": "draft_reply" in payload or bool(failures), "failures": failures},
            "judgments": {"yes_no": yes_no, "choices": choices, "faq": faq_scores},
            "borderline": planned.borderline, "resampled": planned.resampled, "second_opinion": planned.opinion,
            "notes": planned.notes,
            "cost": {"judge_calls": planned.judge_calls, "judge_input_tokens": planned.tokens,
                     "verify_input_tokens": calls.get("verify_in", 0),
                     "draft_in": calls.get("draft_in", 0), "draft_out": calls.get("draft_out", 0),
                     "note_in": calls.get("note_in", 0), "note_out": calls.get("note_out", 0)},
            "seconds": {"decide": round(t_plan, 2), "total": round(total, 2)},
            "posted": False,
        }
    finally:
        _BUSY.release()
