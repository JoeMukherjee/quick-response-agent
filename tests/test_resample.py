"""Resampling: borderline tickets get a second, independent draw combined toward the safer outcome."""
import json

from test_policy import FAQ, IDS, answers

from qra.core.models import Action, Answers, Ticket
from qra.decision.policy import borderline_signals, combine_answers
from qra.decision.questions import ticket_state
from qra.runtime.pipeline import plan_ticket

TICKET = Ticket("X1", "Onderwerp", "Hoe voeg ik een medewerker toe?", "email", "Basis")


class ScriptedJudge:
    """Returns one scripted Answers per draw (sample 0, then sample 1) and records every request."""

    def __init__(self, draws, coverage=(0.95, 0.95)):
        self.draws, self.coverage, self.requests = draws, coverage, []

    def ask(self, state, questions):
        sample = json.loads(state).get("sample", 0)
        self.requests.append((sample, "covers_everything" in questions))
        if "covers_everything" in questions:
            return Answers(noul={"covers_everything": self.coverage[sample]}, tokens=10)
        return self.draws[sample]


def test_clear_signals_are_not_borderline():
    assert borderline_signals(answers(faq={"F02": 0.97}), IDS) == []


def test_probabilities_near_their_thresholds_are_borderline():
    assert "refund_or_dispute" in borderline_signals(answers(faq={"F02": 0.95}, refund_or_dispute=0.33), IDS)
    assert "faq" in borderline_signals(answers(faq={"F02": 0.74}), IDS)
    assert "injection" in borderline_signals(answers(faq={"F02": 0.95}, injection=0.45), IDS)
    assert "customer_blocked" in borderline_signals(answers(faq={"F02": 0.95}, customer_blocked=0.55), IDS)


def test_two_draws_combine_toward_the_safer_outcome():
    a = answers(faq={"F02": 0.9}, refund_or_dispute=0.2, spam=0.9, asks_for_help=0.2, customer_blocked=0.2)
    b = answers(faq={"F02": 0.5}, refund_or_dispute=0.6, spam=0.5, asks_for_help=0.8, customer_blocked=0.7)
    c = combine_answers(a, b)
    assert c.p("refund_or_dispute") == 0.6          # escalation signal: the larger
    assert c.p("customer_blocked") == 0.7           # escalation signal: the larger
    assert c.p("asks_for_help") == 0.8              # a real customer: the larger, so it is not closed
    assert c.p("spam") == 0.5                       # closing is risky: the smaller
    assert c.p("faq_F02") == 0.5                    # replying is risky: the smaller
    assert c.tokens == a.tokens + b.tokens


def test_a_clear_ticket_costs_exactly_one_triage_call_plus_coverage():
    judge = ScriptedJudge([answers(faq={"F02": 0.97}), answers(faq={"F02": 0.97})])
    out = plan_ticket(TICKET, judge, FAQ)
    assert out.plan.action is Action.REPLY and not out.resampled
    assert judge.requests == [(0, False), (0, True)]


def test_a_borderline_help_centre_match_is_asked_again_and_the_lower_draw_wins():
    judge = ScriptedJudge([answers(faq={"F02": 0.73}), answers(faq={"F02": 0.45})])
    out = plan_ticket(TICKET, judge, FAQ)
    assert out.resampled and "faq" in out.borderline
    assert out.plan.action is Action.HANDOFF  # 0.45 after combining: unsure never ends in a reply
    assert [r[0] for r in judge.requests] == [0, 1]  # the second draw is a different input


def test_a_borderline_hard_rule_escalates_when_either_draw_sees_it():
    judge = ScriptedJudge([answers(faq={"F02": 0.97}, refund_or_dispute=0.27), answers(faq={"F02": 0.97}, refund_or_dispute=0.41)])
    out = plan_ticket(TICKET, judge, FAQ)
    assert out.resampled and out.plan.action is Action.HANDOFF and out.plan.row == 3


def test_a_borderline_coverage_check_is_asked_again_and_the_lower_draw_wins():
    judge = ScriptedJudge([answers(faq={"F02": 0.97})] * 2, coverage=(0.74, 0.5))
    out = plan_ticket(TICKET, judge, FAQ)
    assert out.resampled and "coverage" in out.borderline and out.coverage == 0.5
    assert out.plan.action is Action.HANDOFF


def test_resampling_can_be_switched_off():
    judge = ScriptedJudge([answers(faq={"F02": 0.73}), answers(faq={"F02": 0.1})])
    out = plan_ticket(TICKET, judge, FAQ, resample=False)
    assert not out.resampled and out.plan.action is Action.REPLY and judge.requests[0][0] == 0
    assert all(r[0] == 0 for r in judge.requests)


def test_the_second_draw_is_a_different_request_and_so_its_own_cache_entry():
    assert ticket_state(TICKET, sample=1) != ticket_state(TICKET) and json.loads(ticket_state(TICKET, sample=1))["sample"] == 1
    assert "sample" not in json.loads(ticket_state(TICKET))
