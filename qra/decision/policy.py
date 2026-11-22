"""The decision logic: a PURE function from model probabilities to an action.

The model supplies judgments (probabilities of narrow statements); this module owns the decision, as an
ordered truth table, first matching row wins. Nothing here calls a model or the network.

  row 1  empty message (checked in code)                          -> close
  row 2  injection >= 0.5                                         -> handoff
  row 3  any client hard rule >= 0.3                              -> handoff
  row 4  spam >= 0.85 and asks_for_help < 0.3                     -> close
  row 5  language is "other" or below 0.6                         -> handoff
  row 6  priority is P1 or P2                                     -> handoff
  row 7  some help-centre entry resolves the message (>= 0.7)     -> reply CANDIDATE (verified later)
  row 8  anything else                                            -> handoff

Invariant: P1 and P2 always go to a human. Unsure (probability in the 0.3-0.7 band) never ends in a reply.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from qra.core.config import THRESHOLDS as T
from qra.core.models import Action, Answers, Ticket
from qra.decision.questions import HARD_RULES, LANGUAGES, faq_key

PRIORITY_ORDER = ("P1", "P2", "P3", "P4")


@dataclass
class Plan:
    action: Action
    category: str
    priority: str
    language: str
    reason: str
    row: int
    confidence: float
    faq_ids: list[str] = field(default_factory=list)  # candidate entries for the drafter, best first
    triggers: dict[str, float] = field(default_factory=dict)  # the probabilities that decided this row

    @property
    def reply_candidate(self) -> bool:
        return self.action is Action.REPLY


def is_empty(ticket: Ticket) -> bool:
    """No letters in subject and body: nothing for a human or a model to act on."""
    return not re.search(r"[^\W\d_]", f"{ticket.subject} {ticket.body}")


def empty_plan(ticket: Ticket) -> Plan:
    return Plan(Action.CLOSE, "other", "P4", "nl", "Empty ticket: no text to act on.", 1, 1.0)


def language_of(a: Answers) -> tuple[str, float]:
    probs = a.choice["language"]
    best = max(LANGUAGES, key=lambda k: probs.get(k, 0.0))
    return best, probs.get(best, 0.0)


def best_faq(a: Answers, faq_ids: list[str]) -> tuple[str | None, float]:
    scored = [(a.p(faq_key(i)), i) for i in faq_ids if faq_key(i) in a.noul]
    if not scored:
        return None, 0.0
    p, i = max(scored)
    return i, p


def priority_of(a: Answers, faq_ids: list[str]) -> str:
    _, faq_p = best_faq(a, faq_ids)
    category, _ = a.top("category")
    if (a.p("many_users_blocked") >= T["yes"] or a.p("security_incident") >= T["hard_rule"]
            or a.p("legal_threat") >= T["hard_rule"]):
        return "P1"
    money_or_data = any(a.p(k) >= T["hard_rule"] for k in
                        ("refund_or_dispute", "cancel_or_contract", "personal_data_request", "threatens_to_leave"))
    # The 0.3-0.7 band rounds toward the more urgent level, so "blocked" counts from 0.5.
    blocked_without_help = a.p("customer_blocked") >= 0.5 and faq_p < T["faq_resolves"]
    if money_or_data or blocked_without_help:
        return "P2"
    if category == "bug" or a.p("customer_blocked") >= T["unsettled"]:
        return "P3"
    return "P4"


def triage(a: Answers, faq_ids: list[str]) -> Plan:
    category, _ = a.top("category")
    priority = priority_of(a, faq_ids)
    language, lang_p = language_of(a)
    hard = {r.key: a.p(r.key) for r in HARD_RULES}
    worst_rule, worst_p = max(hard.items(), key=lambda kv: kv[1])
    injection = a.p("injection")
    spam, helps = a.p("spam"), a.p("asks_for_help")
    faq_id, faq_p = best_faq(a, faq_ids)

    def plan(action: Action, row: int, reason: str, conf: float, triggers: dict, faq: list | None = None, cat=None):
        return Plan(action, cat or category, priority, language, reason, row, round(conf, 3), faq or [], triggers)

    if injection >= T["injection"]:
        return plan(Action.HANDOFF, 2, "The message tries to instruct the assistant; a human should read it.",
                    injection, {"injection": injection})
    if worst_p >= T["hard_rule"]:
        return plan(Action.HANDOFF, 3,
                    f"Client rule: {worst_rule.replace('_', ' ')} always goes to a human.", worst_p, {worst_rule: worst_p})
    if spam >= T["spam_close"] and helps < T["asks_help_max"]:
        return plan(Action.CLOSE, 4, "Spam: an unsolicited offer unrelated to the product.",
                    min(spam, 1 - helps), {"spam": spam, "asks_for_help": helps}, cat="other")
    if lang_p < T["language_min"] or a.top("language")[0] == "other":
        return plan(Action.HANDOFF, 5, "Language is unsure or not one of nl, en, de.", 1 - lang_p, {"language": lang_p})
    if priority in ("P1", "P2"):
        return plan(Action.HANDOFF, 6, f"{priority}: a person is blocked or money or the contract is involved.",
                    0.9, {"customer_blocked": a.p("customer_blocked"), "many_users_blocked": a.p("many_users_blocked")})
    if faq_id and faq_p >= T["faq_resolves"]:
        ranked = sorted((i for i in faq_ids if a.p(faq_key(i)) >= T["faq_candidate"]),
                        key=lambda i: (-a.p(faq_key(i)), i))[: int(T["max_entries"])]
        conf = min(faq_p, lang_p, 1 - worst_p, 1 - injection)
        return plan(Action.REPLY, 7, f"The help centre ({', '.join(ranked)}) answers this.", conf,
                    {faq_key(i): a.p(faq_key(i)) for i in ranked}, ranked)
    return plan(Action.HANDOFF, 8, "The help centre does not clearly answer this; a human should handle it.",
                1 - faq_p, {"best_faq": faq_p})


def apply_coverage(plan: Plan, coverage: float) -> Plan:
    """Second stage: with only the candidate entries in view, do they answer EVERYTHING that was asked?"""
    if plan.action is not Action.REPLY or coverage >= T["coverage"]:
        plan.confidence = round(min(plan.confidence, coverage), 3) if plan.action is Action.REPLY else plan.confidence
        return plan
    return Plan(Action.HANDOFF, plan.category, plan.priority, plan.language,
                "The help centre only partly answers this; a human should handle it.", 8,
                round(1 - coverage, 3), plan.faq_ids, {**plan.triggers, "covers_everything": coverage})
