"""Stage 1 + 2 of the pipeline: from a ticket to a Plan (no drafting yet).

    redact -> [empty? close] -> triage call (ticket only in state) -> [borderline? ask once more and combine]
           -> policy.triage -> if a reply candidate: coverage call (ticket + candidate entries)
           -> [borderline? ask once more] -> policy.apply_coverage

Resampling: Decision 1 is not fully deterministic, so a probability close to its threshold can flip between
runs. When a deciding probability lands within `resample_margin` of its threshold, the ticket is asked about
once more with a slightly different input (an independent draw) and the two answers are combined toward the
safer outcome (see policy.combine_answers). Clear cases never pay for the second call.

Model failures are errors: JudgeError propagates and the runner leaves the ticket open.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from qra.core.config import THRESHOLDS as T
from qra.core.errors import FatalAuthError
from qra.core.faq import Faq
from qra.core.models import Answers, Ticket
from qra.decision.judge import Judge
from qra.decision.policy import (Plan, apply_coverage, apply_opinion, borderline_signals, combine_answers, empty_plan,
                                 is_empty, opinion_zone, triage)
from qra.decision.questions import DEFAULT_FAQ_MODE, coverage_questions, coverage_state, ticket_state, triage_questions


@dataclass
class Planned:
    ticket: Ticket
    plan: Plan
    triage: Answers | None = None
    coverage: float | None = None
    judge_calls: int = 0
    tokens: int = 0
    resampled: bool = False
    borderline: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    opinion: dict | None = None        # summary of the second opinion, when one was asked for
    shortlist: list[str] | None = None  # entries the judge was asked about (None: all of them)
    opinion_tokens: tuple[int, int] = (0, 0)


def plan_ticket(ticket: Ticket, judge: Judge, faq: Faq, mode: str = DEFAULT_FAQ_MODE, resample: bool = True,
                opinion=None, shortlister=None) -> Planned:
    if is_empty(ticket):
        return Planned(ticket, empty_plan(ticket), notes=["empty ticket, no model call"])
    shortlist = None
    if shortlister is not None:  # cascade retrieval: the judge only sees a bounded set of entries
        cut = shortlister.for_ticket(ticket, faq)
        faq, shortlist = cut.faq, (None if cut.exhaustive else cut.ids)
    questions = triage_questions(faq, mode)
    answers = judge.ask(ticket_state(ticket, mode), questions)
    calls, tokens, resampled = 1, answers.tokens, False
    near = borderline_signals(answers, faq.ids()) if resample else []
    if near:
        again = judge.ask(ticket_state(ticket, mode, sample=1), questions)
        answers = combine_answers(answers, again)
        calls, tokens, resampled = calls + 1, tokens + again.tokens, True
    plan = triage(answers, faq.ids())
    out = Planned(ticket, plan, triage=answers, judge_calls=calls, tokens=tokens, resampled=resampled, borderline=near,
                  shortlist=shortlist)
    if plan.reply_candidate:
        entries = [faq[i] for i in plan.faq_ids]
        cov = judge.ask(coverage_state(ticket, entries), coverage_questions())
        p, extra = cov.p("covers_everything"), cov.tokens
        out.judge_calls += 1
        if resample and abs(p - T["coverage"]) <= T["resample_margin"]:
            again = judge.ask(coverage_state(ticket, entries, sample=1), coverage_questions())
            p = min(p, again.p("covers_everything"))  # replying is the risky action: take the lower of the two
            extra += again.tokens
            out.judge_calls += 1
            out.resampled = True
            out.borderline.append("coverage")
        out.coverage = p
        out.tokens += extra
        if opinion is not None and opinion_zone(p):
            try:
                op = opinion.assess(ticket, entries)
            except FatalAuthError:
                raise
            except Exception as exc:  # the opinion is advisory: if it is unavailable the rule-based decision stands
                out.notes.append(f"second opinion unavailable ({type(exc).__name__}); rule-based decision kept")
                out.plan = apply_coverage(plan, p)
                return out
            out.opinion, out.opinion_tokens = op.summary(), (op.in_tokens, op.out_tokens)
            out.borderline.append("opinion")
            out.plan = apply_opinion(plan, p, op)
        else:
            out.plan = apply_coverage(plan, p)
    return out
