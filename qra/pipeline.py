"""Stage 1 + 2 of the pipeline: from a ticket to a Plan (no drafting yet).

    redact -> [empty? close] -> triage call (ticket only in state) -> policy.triage
           -> if a reply candidate: coverage call (ticket + candidate entries) -> policy.apply_coverage

Model failures are errors: JudgeError propagates and the runner leaves the ticket open.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .faq import Faq
from .judge import Judge
from .models import Answers, Ticket
from .policy import Plan, apply_coverage, empty_plan, is_empty, triage
from .questions import coverage_questions, coverage_state, ticket_state, triage_questions


@dataclass
class Planned:
    ticket: Ticket
    plan: Plan
    triage: Answers | None = None
    coverage: float | None = None
    judge_calls: int = 0
    tokens: int = 0
    notes: list[str] = field(default_factory=list)


def plan_ticket(ticket: Ticket, judge: Judge, faq: Faq) -> Planned:
    if is_empty(ticket):
        return Planned(ticket, empty_plan(ticket), notes=["empty ticket, no model call"])
    answers = judge.ask(ticket_state(ticket), triage_questions(faq))
    plan = triage(answers, faq.ids())
    out = Planned(ticket, plan, triage=answers, judge_calls=1, tokens=answers.tokens)
    if plan.reply_candidate:
        entries = [faq[i] for i in plan.faq_ids]
        cov = judge.ask(coverage_state(ticket, entries), coverage_questions())
        out.coverage = cov.p("covers_everything")
        out.judge_calls += 1
        out.tokens += cov.tokens
        out.plan = apply_coverage(plan, out.coverage)
    return out
