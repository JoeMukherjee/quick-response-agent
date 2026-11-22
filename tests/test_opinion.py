"""Second opinion: the agent proposes, the rule table disposes."""
from types import SimpleNamespace

import pytest
from test_policy import FAQ, IDS, answers
from test_resample import TICKET, ScriptedJudge

from qra.core.errors import FatalAuthError
from qra.core.models import Action
from qra.decision.policy import Plan, apply_coverage, apply_opinion, opinion_clean, opinion_zone, triage
from qra.runtime.pipeline import plan_ticket


def opinion(verdict="answerable", unanswered=(), conflicts=(), used=("F02",), reasoning="ok"):
    return SimpleNamespace(verdict=verdict, unanswered_parts=list(unanswered), conflicting_entry_ids=list(conflicts),
                           entries_used=list(used), reasoning=reasoning, in_tokens=40, out_tokens=60,
                           summary=lambda: {"verdict": verdict})


def reply_plan():
    return triage(answers(faq={"F02": 0.97}), IDS)


class FakeOpinion:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, 0

    def assess(self, ticket, entries):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


# ---- the rule --------------------------------------------------------------------------------------------------

def test_the_zone_is_a_band_around_the_coverage_gate():
    assert not opinion_zone(0.49) and opinion_zone(0.5) and opinion_zone(0.79) and not opinion_zone(0.8)


def test_a_clean_sign_off_needs_every_condition():
    plan = reply_plan()
    assert opinion_clean(plan, opinion())
    assert not opinion_clean(plan, opinion(verdict="partial"))
    assert not opinion_clean(plan, opinion(unanswered=["the price for 500 users"]))
    assert not opinion_clean(plan, opinion(conflicts=["F05"]))
    assert not opinion_clean(plan, opinion(used=[]))
    assert not opinion_clean(plan, opinion(used=["F05"]))  # an entry the table never matched cannot carry a reply


def test_a_veto_turns_a_passing_reply_into_a_handoff():
    plan = apply_opinion(reply_plan(), 0.75, opinion(verdict="partial", unanswered=["delivery date"]))
    assert plan.action is Action.HANDOFF and plan.row == 8 and "delivery date" in plan.reason
    assert plan.triggers["second_opinion"] == 0.0


def test_a_confirming_opinion_leaves_a_passing_reply_alone():
    plan = apply_opinion(reply_plan(), 0.75, opinion())
    assert plan.action is Action.REPLY and plan.row == 7


def test_a_clean_opinion_upgrades_a_coverage_handoff_to_a_reply():
    plan = apply_opinion(reply_plan(), 0.66, opinion())
    assert plan.action is Action.REPLY and plan.row == 7 and plan.confidence <= 0.66
    assert plan.triggers["second_opinion"] == 1.0 and "second opinion" in plan.reason


def test_an_opinion_never_upgrades_from_below_the_floor():
    assert apply_opinion(reply_plan(), 0.45, opinion()).action is Action.HANDOFF


def test_an_unclean_opinion_leaves_a_coverage_handoff_exactly_as_the_rule_made_it():
    for op in (opinion(verdict="partial"), opinion(conflicts=["F05"]), opinion(used=["F05"])):
        got = apply_opinion(reply_plan(), 0.66, op)
        rule = apply_coverage(reply_plan(), 0.66)
        assert (got.action, got.row, got.reason) == (rule.action, rule.row, rule.reason)


def test_the_rule_table_rows_before_coverage_cannot_be_overridden():
    # a client rule fired: the plan is a handoff at row 3 and is never a reply candidate, so no opinion is asked
    judge = ScriptedJudge([answers(faq={"F02": 0.97}, refund_or_dispute=0.9)] * 2)
    agent = FakeOpinion(opinion())
    out = plan_ticket(TICKET, judge, FAQ, opinion=agent)
    assert out.plan.action is Action.HANDOFF and out.plan.row == 3 and agent.calls == 0


# ---- the pipeline ----------------------------------------------------------------------------------------------

def test_without_an_agent_the_pipeline_is_unchanged():
    judge = ScriptedJudge([answers(faq={"F02": 0.97})] * 2, coverage=(0.66, 0.66))
    assert plan_ticket(TICKET, judge, FAQ).plan.action is Action.HANDOFF


def test_the_agent_is_asked_only_inside_the_zone():
    for cov, asked in ((0.95, 0), (0.66, 1), (0.3, 0)):
        judge = ScriptedJudge([answers(faq={"F02": 0.97})] * 2, coverage=(cov, cov))
        agent = FakeOpinion(opinion())
        plan_ticket(TICKET, judge, FAQ, opinion=agent)
        assert agent.calls == asked, cov


def test_an_agent_that_confirms_turns_a_borderline_handoff_into_a_reply_and_its_tokens_are_counted():
    judge = ScriptedJudge([answers(faq={"F02": 0.97})] * 2, coverage=(0.66, 0.66))
    out = plan_ticket(TICKET, judge, FAQ, opinion=FakeOpinion(opinion()))
    assert out.plan.action is Action.REPLY and out.opinion == {"verdict": "answerable"}
    assert out.opinion_tokens == (40, 60) and "opinion" in out.borderline


def test_an_agent_that_objects_stops_a_reply_the_coverage_check_would_have_sent():
    judge = ScriptedJudge([answers(faq={"F02": 0.97})] * 2, coverage=(0.78, 0.78))
    out = plan_ticket(TICKET, judge, FAQ, opinion=FakeOpinion(opinion(verdict="partial", unanswered=["a refund"])))
    assert out.plan.action is Action.HANDOFF


def test_an_agent_that_fails_never_fails_the_ticket():
    judge = ScriptedJudge([answers(faq={"F02": 0.97})] * 2, coverage=(0.78, 0.78))
    out = plan_ticket(TICKET, judge, FAQ, opinion=FakeOpinion(error=RuntimeError("503")))
    assert out.plan.action is Action.REPLY and any("unavailable" in n for n in out.notes)  # the rule-based result stands


def test_an_invalid_key_still_stops_everything():
    judge = ScriptedJudge([answers(faq={"F02": 0.97})] * 2, coverage=(0.66, 0.66))
    with pytest.raises(FatalAuthError):
        plan_ticket(TICKET, judge, FAQ, opinion=FakeOpinion(error=FatalAuthError("bad key")))
