"""Verification of a drafted reply before it can reach a customer.

Two independent layers, both must pass, otherwise the ticket goes to a human with the draft attached:
  1. deterministic checks (checks.py): numbers, links, menu paths, contact and payment data;
  2. model judgments over {ticket, matched entries, reply}: one yes/no per reply sentence ("does it state something that is
     in neither the help centre nor the customer's message?"), plus promise, relevance and language checks.
     One judgment per question: "unsupported" is a single claim, so a greeting or closing is simply "no". The earlier
     "supported OR greeting" wording rejected 16/16 clean replies on one backend and 1/16 on the other; this wording 2/16 and
     0/16, with the same 45/48 invented claims caught (scratchpad experiment verify_ab.py, results in eval/results).
The English gloss in the log is for a human reader; it is not a gate.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from qra.safety.checks import run_checks
from qra.core.config import THRESHOLDS as T
from qra.core.faq import FaqEntry
from qra.decision.judge import Judge
from qra.core.models import Ticket
from qra.decision.questions import ticket_state

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text) if len(s.strip()) > 1]


@dataclass
class VerifyResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    tokens: int = 0


def _state(ticket: Ticket, entries: list[FaqEntry], reply: str) -> str:
    base = json.loads(ticket_state(ticket))
    base["help_centre"] = {e.id: e.text for e in entries}
    base["reply"] = reply
    return json.dumps(base, ensure_ascii=False)


_PLEASANTRY = re.compile(
    r"^(hi|hello|hey|dear|hoi|hallo|beste|goedemorgen|goedemiddag|goedenavond|guten tag|liebe|sehr geehrte|"
    r"thanks|thank you|many thanks|bedankt|dank|vielen dank|danke|"
    r"kind regards|best regards|regards|with kind regards|greetings|have a (great|nice|good)|enjoy|good luck|"
    r"groet|groeten|met vriendelijke groet|vriendelijke groet|fijne dag|prettige dag|succes|veel succes|"
    r"mit freundlichen gr\w*|freundliche gr\w*|viele gr\w*|gr(ü|u)(ß|ss)e|einen schönen|schönen tag|viel erfolg)\b",
    re.I)


def is_pleasantry(sentence: str) -> bool:
    """A short greeting or closing that cannot state a fact, step, price, link or condition. It is decided here, in code,
    before any model is asked (a deterministic check first, as the TypeSafe guidance says): the model judged such sentences
    unpredictably, and a good reply was rejected because of "Have a great day!"."""
    s = sentence.strip()
    m = _PLEASANTRY.match(s)
    if not m or re.search(r"[0-9@/→€$%]|https?:|www\.|\.(nl|com|de|eu)", s):
        return False
    return len(re.findall(r"\w+", s[m.end():])) <= 3  # after the greeting only a name, "Roosterly" or a short thanks may remain


def verify_questions(reply: str) -> dict:
    q: dict = {}
    for i, sentence in enumerate(split_sentences(reply)):
        if is_pleasantry(sentence):
            continue
        q[f"u{i}"] = {
            "type": "noul",
            "instructions": (f"Does this sentence of `reply` state a fact, step, price, link or condition that is written neither in "
                             f"`help_centre` nor in `ticket.message`?\n\nSentence: {sentence}"),
            "criteria": {"true_description": "It claims something the help-centre text and the customer's message do not say.",
                         "false_description": ("Everything it says is in the help-centre text or the customer's message, or it follows directly "
                                               "from them, or it is a greeting, a closing or a restatement of the customer's question.")}}
    q["promise"] = {
        "type": "noul",
        "instructions": "Does `reply` promise an action, a date, a refund, a discount or a way to contact someone that `help_centre` does not state?",
        "criteria": {"true_description": "The reply commits the company to something that help_centre does not say.",
                     "false_description": "The reply only gives information that help_centre contains."}}
    q["responsive"] = {
        "type": "noul",
        "instructions": "Does `reply` respond to what the customer wrote in `ticket.message`?",
        "criteria": {"true_description": "The reply addresses the customer's question or request.",
                     "false_description": "The reply is about something else or ignores the customer."}}
    q["reply_language"] = {
        "type": "choice",
        "instructions": "Which language is `reply` written in?",
        "criteria": {"nl": "Dutch", "en": "English", "de": "German", "other": "Any other language."}}
    return q


def verify_reply(ticket: Ticket, entries: list[FaqEntry], reply: str, language: str, judge: Judge) -> VerifyResult:
    failures = run_checks(reply, entries, f"{ticket.subject} {ticket.body}")
    answers = judge.ask(_state(ticket, entries, reply), verify_questions(reply))
    for key, p in answers.noul.items():
        if re.fullmatch(r"u\d+", key) and p >= T["verify_unsupported"]:
            failures.append(f"sentence {key[1:]} may state something the help centre does not (p={p:.2f})")
    if answers.noul["promise"] >= T["verify_promise"]:
        failures.append(f"reply may promise something the help centre does not (p={answers.noul['promise']:.2f})")
    if answers.noul["responsive"] < T["yes"]:
        failures.append(f"reply may not address the customer's message (p={answers.noul['responsive']:.2f})")
    lang, lang_p = answers.top("reply_language")
    if lang != language or lang_p < T["language_min"]:
        failures.append(f"reply language looks like {lang} ({lang_p:.2f}), expected {language}")
    return VerifyResult(not failures, failures, answers.tokens)
