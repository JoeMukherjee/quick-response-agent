"""The rule table, proven rather than sampled.

TypeSafe's guidance: give every combination of answers an outcome, including a probability that lands between thresholds.
These tests walk a grid of judgments that straddles every threshold and check, for each combination, that

  * there is exactly one defined outcome (totality: no combination falls through);
  * the safety properties in docs/RULES.md hold (a reply needs everything clear; raising a risk signal can only escalate);
  * the priority follows the brief's definitions.
"""
import itertools
import re
from pathlib import Path

import pytest
from test_policy import IDS, answers

from qra.core.config import THRESHOLDS as T
from qra.core.models import Action
from qra.decision.policy import PRIORITY_MEANING, explain, priority_of, triage
from qra.decision.questions import HARD_RULES

HARD = [r.key for r in HARD_RULES]
# values chosen to sit on both sides of every threshold in config.THRESHOLDS
INJECTION = (0.05, 0.45, 0.55)
HARD_RULE = (0.05, 0.29, 0.31, 0.9)
SPAM = (0.05, 0.84, 0.86)
HELPS = (0.1, 0.35, 0.95)
LANG = ("nl", "other")
MANY = (0.05, 0.69, 0.71)
BLOCKED = (0.05, 0.31, 0.55, 0.9)
FAQ = (0.1, 0.5, 0.69, 0.71, 0.95)
CATEGORY = ("question", "bug")


def grid():
    for inj, hard, spam, helps, lang, many, blocked, faq, cat in itertools.product(INJECTION, HARD_RULE, SPAM, HELPS, LANG, MANY, BLOCKED, FAQ, CATEGORY):
        yield dict(injection=inj, hard=hard, spam=spam, helps=helps, lang=lang, many=many, blocked=blocked, faq=faq, cat=cat)


def build(g, **override):
    g = {**g, **override}
    noul = {"injection": g["injection"], "spam": g["spam"], "asks_for_help": g["helps"], "many_users_blocked": g["many"],
            "customer_blocked": g["blocked"], "refund_or_dispute": g["hard"]}
    return answers(lang=g["lang"] if g["lang"] != "other" else "nl", category=g["cat"], faq={"F02": g["faq"]}, **noul), g


def run(g):
    a, g = build(g)
    if g["lang"] == "other":  # the language question says "other": make it the top option
        a.choice["language"] = {"nl": 0.02, "en": 0.02, "de": 0.02, "other": 0.94}
    return triage(a, IDS)


def test_every_combination_has_exactly_one_defined_outcome():
    n = 0
    for g in grid():
        plan = run(g)
        assert plan.action in (Action.REPLY, Action.HANDOFF, Action.CLOSE), g
        assert plan.row in (2, 3, 4, 5, 6, 7, 8) and plan.reason and plan.priority in PRIORITY_MEANING, g
        assert explain(plan)  # and every one can be explained in the posted reason
        n += 1
    assert n == len(INJECTION) * len(HARD_RULE) * len(SPAM) * len(HELPS) * len(LANG) * len(MANY) * len(BLOCKED) * len(FAQ) * len(CATEGORY)


def test_a_reply_candidate_needs_every_signal_to_be_clear():
    replies = [(g, run(g)) for g in grid() if run(g).action is Action.REPLY]
    assert replies  # the table does reply when everything is clear
    for g, plan in replies:
        assert g["injection"] < T["injection"] and g["hard"] < T["hard_rule"] and g["lang"] != "other"
        assert plan.priority in ("P3", "P4") and g["faq"] >= T["faq_resolves"], g


def test_close_needs_spam_and_a_sender_who_is_not_asking_for_help_and_no_client_rule():
    for g in grid():
        plan = run(g)
        if plan.action is Action.CLOSE:
            assert g["spam"] >= T["spam_close"] and g["helps"] < T["asks_help_max"] and g["injection"] < T["injection"] and g["hard"] < T["hard_rule"], g


@pytest.mark.parametrize("signal,values", [("injection", INJECTION), ("hard", HARD_RULE), ("many", MANY), ("blocked", BLOCKED)])
def test_raising_a_risk_signal_can_only_escalate_never_turn_a_handoff_into_a_reply_or_a_close(signal, values):
    for g in grid():
        low = run({**g, signal: values[0]})
        if low.action is not Action.HANDOFF:
            continue
        for higher in values[1:]:
            assert run({**g, signal: higher}).action is Action.HANDOFF, (signal, higher, g)


def test_a_weaker_help_centre_match_can_never_turn_a_handoff_into_a_reply():
    for g in grid():
        if run(g).action is Action.HANDOFF:
            for weaker in (v for v in FAQ if v < g["faq"]):
                assert run({**g, "faq": weaker}).action is Action.HANDOFF, (weaker, g)


# ---- priority follows the brief's definitions ----------------------------------------------------------------------

def priority(**noul):
    return priority_of(answers(**noul), IDS)


@pytest.mark.parametrize("key", HARD + ["threatens_to_leave"])
def test_money_or_the_contract_is_never_below_p2(key):
    assert priority(**{key: 0.31}) in ("P1", "P2")


def test_security_and_legal_risk_and_many_users_are_p1():
    assert priority(security_incident=0.31) == "P1" and priority(legal_threat=0.31) == "P1" and priority(many_users_blocked=0.71) == "P1"


def test_a_cosmetic_issue_or_a_how_to_is_never_above_p3_and_usually_p4():
    assert priority(category="bug", cosmetic_only=0.9) == "P4"
    assert priority(faq={"F02": 0.95}, customer_blocked=0.4) == "P4"


# ---- the rules are written down, and the document cannot drift from the code ---------------------------------------

RULES = (Path(__file__).resolve().parent.parent / "docs" / "RULES.md").read_text(encoding="utf-8")


def test_every_threshold_is_documented_with_its_current_value():
    for key, value in T.items():
        assert f"`{key}`" in RULES, f"threshold {key} is not explained in docs/RULES.md"
        assert re.search(rf"`{re.escape(key)}`[^\n]*\b{re.escape(str(value))}\b", RULES), f"docs/RULES.md shows a different value for {key} (code: {value})"


def test_every_question_and_every_row_is_documented():
    from qra.core.faq import load_faq
    from qra.decision.questions import triage_questions
    for key in (k for k in triage_questions(load_faq()) if not k.startswith("faq_")):
        assert f"`{key}`" in RULES, f"question {key} is not explained in docs/RULES.md"
    for row in range(1, 9):
        assert re.search(rf"\|\s*{row}\s*\|", RULES), f"truth-table row {row} is not in docs/RULES.md"
