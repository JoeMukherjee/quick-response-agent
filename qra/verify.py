"""Verification of a drafted reply before it can reach a customer.

Two independent layers, both must pass, otherwise the ticket goes to a human with the draft attached:
  1. deterministic checks (checks.py): numbers, links, menu paths, contact and payment data;
  2. Decision-1 questions over {ticket, matched entries, reply}: one yes/no per reply sentence
     ("supported by the help centre, or only a greeting/closing"), plus promise, relevance and language checks.
The English gloss in the log is for a human reader; it is not a gate.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .checks import run_checks
from .config import THRESHOLDS as T
from .faq import FaqEntry
from .judge import Judge
from .models import Ticket
from .questions import ticket_state

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


def verify_questions(reply: str) -> dict:
    q: dict = {}
    for i, sentence in enumerate(split_sentences(reply)):
        q[f"s{i}"] = {
            "type": "noul",
            "instructions": (f"Is this sentence of `reply` supported by `help_centre`, or is it only a greeting, "
                             f"a closing, or a restatement of what the customer asked?\n\nSentence: {sentence}"),
            "criteria": {"true_description": "Every fact in the sentence is stated in help_centre, or the sentence states no facts.",
                         "false_description": "The sentence states a fact, step, price, link or condition that is not in help_centre."}}
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
        if re.fullmatch(r"s\d+", key) and p < T["verify_sentence"]:
            failures.append(f"sentence {key[1:]} is not clearly supported by the help centre (p={p:.2f})")
    if answers.noul["promise"] >= T["verify_promise"]:
        failures.append(f"reply may promise something the help centre does not (p={answers.noul['promise']:.2f})")
    if answers.noul["responsive"] < T["yes"]:
        failures.append(f"reply may not address the customer's message (p={answers.noul['responsive']:.2f})")
    lang, lang_p = answers.top("reply_language")
    if lang != language or lang_p < T["language_min"]:
        failures.append(f"reply language looks like {lang} ({lang_p:.2f}), expected {language}")
    return VerifyResult(not failures, failures, answers.tokens)
