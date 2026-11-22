"""Table tests with synthetic probabilities: one per truth-table row, plus the priority invariant."""
from qra.core.faq import load_faq
from qra.core.models import Action, Answers
from qra.decision.policy import apply_coverage, priority_of, triage
from qra.decision.questions import HARD_RULES, faq_key

FAQ = load_faq()
IDS = FAQ.ids()


def answers(lang="nl", category="question", faq=None, **noul):
    base = {r.key: 0.02 for r in HARD_RULES}
    base.update(injection=0.02, spam=0.02, asks_for_help=0.97, many_users_blocked=0.02, customer_blocked=0.05)
    base.update({faq_key(i): 0.03 for i in IDS})
    base.update({faq_key(k): v for k, v in (faq or {}).items()})
    base.update(noul)
    other = (1 - 0.97) / 3
    lang_probs = {k: other for k in ("nl", "en", "de", "other")}
    lang_probs[lang] = 0.97
    cats = {k: 0.01 for k in ("bug", "billing", "feature_request", "question", "other")}
    cats[category] = 0.96
    return Answers(noul=base, choice={"language": lang_probs, "category": cats})


def test_clear_faq_question_is_a_reply_candidate_with_its_entry():
    plan = triage(answers(faq={"F02": 0.95}), IDS)
    assert plan.action is Action.REPLY and plan.faq_ids == ["F02"] and plan.priority == "P4" and plan.row == 7


def test_bug_answered_by_the_help_centre_is_a_p3_reply():
    plan = triage(answers(category="bug", faq={"F12": 0.9}, customer_blocked=0.8), IDS)
    assert plan.action is Action.REPLY and plan.priority == "P3"


def test_feature_request_with_a_roadmap_entry_is_a_reply():
    assert triage(answers(category="feature_request", faq={"F10": 0.85}), IDS).action is Action.REPLY


def test_two_entries_are_both_passed_to_the_drafter():
    plan = triage(answers(faq={"F05": 0.9, "F07": 0.6}), IDS)
    assert plan.faq_ids == ["F05", "F07"]


def test_blocked_with_no_help_centre_answer_is_p2_handoff():
    plan = triage(answers(category="bug", customer_blocked=0.9), IDS)
    assert plan.action is Action.HANDOFF and plan.priority == "P2" and plan.row == 6


def test_no_faq_match_goes_to_a_human():
    plan = triage(answers(), IDS)
    assert plan.action is Action.HANDOFF and plan.row == 8


def test_each_hard_rule_forces_handoff_even_when_the_faq_matches():
    for rule in HARD_RULES:
        plan = triage(answers(faq={"F05": 0.95}, **{rule.key: 0.35}), IDS)
        assert plan.action is Action.HANDOFF and plan.row == 3, rule.key


def test_injection_forces_handoff_first():
    plan = triage(answers(faq={"F02": 0.95}, injection=0.6, refund_or_dispute=0.9), IDS)
    assert plan.action is Action.HANDOFF and plan.row == 2


def test_spam_closes_only_when_clearly_not_a_customer():
    assert triage(answers(spam=0.95, asks_for_help=0.05), IDS).action is Action.CLOSE
    assert triage(answers(spam=0.95, asks_for_help=0.6), IDS).action is not Action.CLOSE
    assert triage(answers(spam=0.8, asks_for_help=0.05), IDS).action is not Action.CLOSE


def test_spam_that_also_trips_a_hard_rule_is_never_closed():
    assert triage(answers(spam=0.99, asks_for_help=0.01, legal_threat=0.5), IDS).action is Action.HANDOFF


def test_unsure_or_other_language_goes_to_a_human():
    assert triage(answers(lang="other", faq={"F02": 0.95}), IDS).row == 5
    a = answers(faq={"F02": 0.95})
    a.choice["language"].update({"nl": 0.5, "en": 0.4, "de": 0.05, "other": 0.05})
    assert triage(a, IDS).action is Action.HANDOFF


def test_priority_levels():
    assert priority_of(answers(many_users_blocked=0.9), IDS) == "P1"
    assert priority_of(answers(security_incident=0.4), IDS) == "P1"
    assert priority_of(answers(personal_data_request=0.5), IDS) == "P2"
    assert priority_of(answers(category="bug"), IDS) == "P3"
    assert priority_of(answers(), IDS) == "P4"


def test_invariant_p1_and_p2_never_reply():
    seen = set()
    for noul in ({"many_users_blocked": 0.9}, {"customer_blocked": 0.9}, {"refund_or_dispute": 0.4},
                 {"customer_blocked": 0.55}, {"security_incident": 0.35}, {}):
        for faq_p in (0.0, 0.6, 0.99):
            plan = triage(answers(faq={"F02": faq_p}, **noul), IDS)
            seen.add(plan.priority)
            if plan.priority in ("P1", "P2"):
                assert plan.action is Action.HANDOFF, (noul, faq_p)
    assert {"P1", "P2"} <= seen  # the loop really exercised both urgent levels


def test_blocked_but_fully_answered_by_the_help_centre_is_p3_and_may_reply():
    plan = triage(answers(faq={"F02": 0.99}, customer_blocked=0.9), IDS)
    assert plan.priority == "P3" and plan.action is Action.REPLY


def test_unsettled_probability_never_ends_in_a_reply():
    plan = triage(answers(faq={"F02": 0.6}), IDS)  # in the 0.3-0.7 band
    assert plan.action is Action.HANDOFF


def test_coverage_stage_downgrades_partial_answers_to_handoff():
    plan = triage(answers(faq={"F02": 0.95}), IDS)
    assert apply_coverage(plan, 0.9).action is Action.REPLY
    assert apply_coverage(triage(answers(faq={"F02": 0.95}), IDS), 0.4).action is Action.HANDOFF


# ---- priority follows the brief's definitions ----------------------------------------------------------------------

def prio(category="question", faq=None, **noul):
    from qra.decision.policy import priority_of
    return priority_of(answers(category=category, faq=faq or {}, **noul), IDS)


def test_a_how_to_the_help_centre_fully_answers_is_p4_even_if_the_customer_sounds_a_little_stuck():
    assert prio(faq={"F06": 0.96}, customer_blocked=0.4) == "P4"
    assert prio(faq={"F06": 0.96}, customer_blocked=0.05) == "P4"


def test_someone_clearly_blocked_who_is_given_the_answer_has_a_workaround_which_is_p3():
    assert prio(faq={"F01": 0.96}, customer_blocked=0.6) == "P3"


def test_stuck_and_not_answered_is_p2_and_unclear_is_p3():
    assert prio(faq={}, customer_blocked=0.7) == "P2"   # one customer blocked
    assert prio(faq={}, customer_blocked=0.4) == "P3"   # unclear, no answer in the help centre


def test_a_bug_is_p3_only_when_something_fails_and_a_cosmetic_one_is_p4():
    assert prio(category="bug", cosmetic_only=0.05) == "P3"
    assert prio(category="bug", cosmetic_only=0.9) == "P4"   # a typo on a button is a bug report but a cosmetic issue
    assert prio(category="bug") == "P3"                        # answers recorded before the question existed read as not cosmetic


def test_money_security_and_many_users_still_outrank_everything():
    assert prio(refund_or_dispute=0.9, cosmetic_only=0.9) == "P2"
    assert prio(many_users_blocked=0.9) == "P1"
    assert prio(security_incident=0.5) == "P1"


# ---- the reason field says why the category, the priority and the action -------------------------------------------

def test_the_posted_reason_names_the_category_and_the_priority_in_two_sentences():
    from qra.decision.policy import explain, triage
    plan = triage(answers(category="question", faq={"F06": 0.96}), IDS)
    reason = explain(plan)
    assert "F06" in reason and "Category question" in reason and "priority P4" in reason and "how-to" in reason
    assert reason.count(". ") + reason.count(".") <= 4 and len(reason) < 260  # one or two sentences, not an essay


def test_a_priority_row_does_not_state_the_priority_twice():
    from qra.decision.policy import explain, triage
    reason = explain(triage(answers(category="bug", customer_blocked=0.9), IDS))
    assert reason.startswith("P2:") and reason.count("P2") == 1 and "Category: bug" in reason


def test_a_client_rule_handoff_keeps_its_rule_and_adds_category_and_priority():
    from qra.decision.policy import explain, triage
    reason = explain(triage(answers(category="billing", refund_or_dispute=0.95), IDS))
    assert "refund" in reason.lower() and "Category billing" in reason and "priority P2" in reason
