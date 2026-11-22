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
from dataclasses import dataclass, field, replace

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


_MAIL_MARKER = re.compile(r"\b(?:re|fw|fwd|aw|wg|sv|antw|tr)\s*:", re.I)  # "Re:", "Fwd:", "AW:" ... added by mail clients


def is_empty(ticket: Ticket) -> bool:
    """No letters in subject and body once reply and forward markers are ignored: nothing to act on, so it is closed.
    A subject of just "Re:" over an empty body is an empty ticket, not a cancellation."""
    return not re.search(r"[^\W\d_]", _MAIL_MARKER.sub(" ", f"{ticket.subject} {ticket.body}"))


def empty_plan(ticket: Ticket) -> Plan:
    return Plan(Action.CLOSE, "other", "P4", "nl", "Empty ticket: no text to act on.", 1, 1.0)


def language_of(a: Answers) -> tuple[str, float]:
    probs = a.choice["language"]
    best = max(LANGUAGES, key=lambda k: probs.get(k, 0.0))
    return best, probs.get(best, 0.0)


def faq_scores(a: Answers, faq_ids: list[str]) -> dict[str, float]:
    """Probability that each help-centre entry is the appropriate response (one question per entry, or one choice)."""
    if "faq_pick" in a.choice:
        return {i: a.choice["faq_pick"].get(i, 0.0) for i in faq_ids}
    return {i: a.p(faq_key(i)) for i in faq_ids if faq_key(i) in a.noul}


def _thresholds(a: Answers) -> tuple[float, float]:
    """(resolves, candidate). One choice splits its probability across entries, so its bars are lower."""
    if "faq_pick" in a.choice:
        return T["faq_resolves_pick"], T["faq_candidate_pick"]
    return T["faq_resolves"], T["faq_candidate"]


def best_faq(a: Answers, faq_ids: list[str]) -> tuple[str | None, float]:
    scored = [(p, i) for i, p in faq_scores(a, faq_ids).items()]
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
    blocked_without_help = a.p("customer_blocked") >= 0.5 and faq_p < _thresholds(a)[0]
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
    resolves, candidate = _thresholds(a)
    scores = faq_scores(a, faq_ids)
    if faq_id and faq_p >= resolves:
        ranked = sorted((i for i, p in scores.items() if p >= candidate),
                        key=lambda i: (-scores[i], i))[: int(T["max_entries"])]
        conf = min(faq_p, lang_p, 1 - worst_p, 1 - injection)
        return plan(Action.REPLY, 7, f"The help centre ({', '.join(ranked)}) answers this.", conf,
                    {faq_key(i): scores[i] for i in ranked}, ranked)
    return plan(Action.HANDOFF, 8, "The help centre does not clearly answer this; a human should handle it.",
                1 - faq_p, {"best_faq": faq_p})


def borderline_signals(a: Answers, faq_ids: list[str]) -> list[str]:
    """Deciding probabilities that sit within the resample margin of their threshold."""
    m = T["resample_margin"]
    near = lambda p, thr: abs(p - thr) <= m  # noqa: E731
    out: list[str] = []
    if near(a.p("injection"), T["injection"]):
        out.append("injection")
    out += [r.key for r in HARD_RULES if near(a.p(r.key), T["hard_rule"])]
    if a.p("spam") >= T["spam_close"] - m and (near(a.p("spam"), T["spam_close"]) or near(a.p("asks_for_help"), T["asks_help_max"])):
        out.append("spam")
    if near(a.p("many_users_blocked"), T["yes"]):
        out.append("many_users_blocked")
    if near(a.p("customer_blocked"), 0.5):
        out.append("customer_blocked")
    if near(language_of(a)[1], T["language_min"]):
        out.append("language")
    resolves, _ = _thresholds(a)
    _, best = best_faq(a, faq_ids)
    if best and near(best, resolves):
        out.append("faq")
    return out


# Which way is safer for each signal when two draws disagree.
_ESCALATE_UP = {r.key for r in HARD_RULES} | {"injection", "many_users_blocked", "customer_blocked", "asks_for_help"}


def combine_answers(a: Answers, b: Answers) -> Answers:
    """Two draws of the same questions, combined toward the safer outcome: escalation signals take the larger
    probability; signals that enable a reply or a close (help-centre match, spam) take the smaller; the rest average."""
    noul: dict[str, float] = {}
    for k, pa in a.noul.items():
        pb = b.noul.get(k, pa)
        if k in _ESCALATE_UP:
            noul[k] = max(pa, pb)
        elif k == "spam" or k.startswith("faq_"):
            noul[k] = min(pa, pb)
        else:
            noul[k] = (pa + pb) / 2
    choice: dict[str, dict[str, float]] = {}
    for k, pa in a.choice.items():
        pb = b.choice.get(k, pa)
        if k == "faq_pick":  # replying is the risky action: the lower of the two draws for every entry
            choice[k] = {o: min(p, pb.get(o, p)) for o, p in pa.items()}
        else:
            choice[k] = {o: (p + pb.get(o, p)) / 2 for o, p in pa.items()}
    return Answers(noul=noul, choice=choice, tokens=a.tokens + b.tokens)


def apply_coverage(plan: Plan, coverage: float) -> Plan:
    """Second stage: with only the candidate entries in view, do they answer EVERYTHING that was asked?"""
    if plan.action is not Action.REPLY or coverage >= T["coverage"]:
        plan.confidence = round(min(plan.confidence, coverage), 3) if plan.action is Action.REPLY else plan.confidence
        return plan
    return Plan(Action.HANDOFF, plan.category, plan.priority, plan.language,
                "The help centre only partly answers this; a human should handle it.", 8,
                round(1 - coverage, 3), plan.faq_ids, {**plan.triggers, "covers_everything": coverage})


def opinion_zone(coverage: float) -> bool:
    """The grey zone around the coverage gate where a second opinion is worth its tokens."""
    return T["opinion_floor"] <= coverage < T["opinion_ceiling"]


def opinion_clean(plan: Plan, op) -> bool:
    """A sign-off the table will accept: every part answered, nothing conflicting, and only the entries that were
    already matched (so the drafter and the verifier work from the same entries the opinion approved)."""
    return (op.verdict == "answerable" and not op.unanswered_parts and not op.conflicting_entry_ids
            and bool(op.entries_used) and set(op.entries_used) <= set(plan.faq_ids))


def apply_opinion(plan: Plan, coverage: float, op) -> Plan:
    """Coverage stage with a second opinion. `plan` is a reply candidate: it already passed every client rule,
    injection, language and priority row, and the opinion cannot move any of those.

      coverage passed, opinion not clean   -> handoff (a veto is always allowed)
      coverage failed, opinion clean       -> reply, but never from below the floor, and the reply is still drafted
                                              and verified like any other
      otherwise                            -> exactly what the coverage rule alone would have decided
    """
    rule_based = apply_coverage(replace(plan, triggers=dict(plan.triggers)), coverage)
    clean = opinion_clean(plan, op)
    note = (op.unanswered_parts or op.conflicting_entry_ids or [op.reasoning])[0]
    if rule_based.action is Action.REPLY and not clean:
        return Plan(Action.HANDOFF, plan.category, plan.priority, plan.language,
                    f"A second opinion found the help centre does not fully answer this: {str(note)[:160]}", 8,
                    round(1 - coverage, 3), plan.faq_ids, {**plan.triggers, "covers_everything": coverage, "second_opinion": 0.0})
    if rule_based.action is Action.HANDOFF and clean and coverage >= T["opinion_floor"]:
        return Plan(Action.REPLY, plan.category, plan.priority, plan.language,
                    f"The help centre ({', '.join(plan.faq_ids)}) answers this; the coverage check was unsure and a second opinion confirmed every part is covered.",
                    7, round(min(plan.confidence, coverage), 3), plan.faq_ids,
                    {**plan.triggers, "covers_everything": coverage, "second_opinion": 1.0})
    if rule_based.action is Action.REPLY:
        rule_based.triggers = {**rule_based.triggers, "second_opinion": 1.0}
    return rule_based
