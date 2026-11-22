"""Deterministic checks on a drafted reply. Pure functions, no model, no network.

A reply may only say what the matched help-centre entries say. These checks catch the failures that a
reader who does not read Dutch could not: invented numbers, invented links, invented menu paths, contact
details that are not in the help centre, and payment data. Each returns a list of human-readable failures
(empty list = pass), so the log can say exactly why a draft was rejected.
"""
from __future__ import annotations

import itertools
import re

from qra.core.faq import FaqEntry
from qra.safety.redact import contains_payment_data

_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:[.\s]\d{3})+|\d+)(?:[,.](\d{1,2}))?(?![\w])")
_URL = re.compile(r"\b(?:https?://)?(?:[a-z0-9-]+\.)+(?:nl|com|io|org|net|eu|de)(?:/[^\s,;)]*)?", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d\s\-()]{7,}\d)(?!\d)")
_QUOTED = re.compile(r"[\"“„']([^\"”“'\n]{3,60})[\"”']")
_ARROW = re.compile(r"[→>]")


def _numbers(text: str) -> set[float]:
    out: set[float] = set()
    for m in _NUMBER.finditer(text):
        whole = re.sub(r"[.\s]", "", m.group(1))
        out.add(float(f"{whole}.{m.group(2)}") if m.group(2) else float(whole))
    return out


def numbers_ok(reply: str, entries: list[FaqEntry], ticket_text: str) -> list[str]:
    """Every number in the reply must come from the entries or the ticket, or be a simple derived value:
    a product or sum of one number from the ticket and one from the entries (e.g. 25 employees x EUR 6)."""
    from_entries = set().union(*(_numbers(e.text) for e in entries)) if entries else set()
    from_ticket = _numbers(ticket_text)
    allowed = set(from_entries) | from_ticket
    for a, b in itertools.product(from_ticket, from_entries):
        allowed |= {round(a * b, 2), round(a + b, 2)}
    for a, b in itertools.product(from_ticket, repeat=2):
        allowed.add(round(a * b, 2))
    bad = sorted(n for n in _numbers(reply) if round(n, 2) not in {round(x, 2) for x in allowed})
    return [f"number {n:g} is not in the help centre or the ticket" for n in bad]


def urls_ok(reply: str, entries: list[FaqEntry]) -> list[str]:
    haystack = " ".join(e.text for e in entries).lower()
    return [f"link {u} is not in the matched help-centre entries"
            for u in {m.group(0).rstrip(".").lower() for m in _URL.finditer(reply)}
            if u.removeprefix("https://").removeprefix("http://") not in haystack]


def paths_ok(reply: str, entries: list[FaqEntry], ticket_text: str) -> list[str]:
    """Menu names and quoted UI labels must appear verbatim in the entries (or be the customer's own words)."""
    haystack = " ".join(e.text for e in entries).lower()
    own = ticket_text.lower()
    fails = []
    for q in _QUOTED.findall(reply):
        q = q.strip()
        if q.lower() not in haystack and q.lower() not in own:
            fails.append(f"quoted label {q!r} is not in the matched help-centre entries")
    for line in reply.splitlines():
        if not _ARROW.search(line):
            continue
        segs = re.split(r"[→>]", line)
        for i, seg in enumerate(segs):
            words = re.findall(r"[^\W\d_][\w'-]*", seg.split("(")[0])
            if not words:
                continue
            # The menu name sits next to the arrow: the last word of the text before it, the first word after it,
            # and the first and last word of any step in between.
            edge = [words[-1]] if i == 0 else [words[0]] if i == len(segs) - 1 else [words[0], words[-1]]
            for w in edge:
                if len(w) >= 3 and w.lower() not in haystack and w.lower() not in own:
                    fails.append(f"menu step {w!r} is not in the matched help-centre entries")
    return fails


def contact_ok(reply: str, entries: list[FaqEntry], ticket_text: str) -> list[str]:
    haystack = " ".join(e.text for e in entries).lower() + " " + ticket_text.lower()
    fails = [f"email {m.group(0)} is not in the help centre" for m in _EMAIL.finditer(reply)
             if m.group(0).lower() not in haystack]
    fails += [f"phone number {m.group(0).strip()} is not in the help centre" for m in _PHONE.finditer(reply)
              if re.sub(r"\D", "", m.group(0)) not in re.sub(r"\D", "", haystack)]
    return fails


def payment_ok(reply: str) -> list[str]:
    return ["reply contains payment data"] if contains_payment_data(reply) else []


def run_checks(reply: str, entries: list[FaqEntry], ticket_text: str) -> list[str]:
    return (payment_ok(reply) + numbers_ok(reply, entries, ticket_text) + urls_ok(reply, entries)
            + paths_ok(reply, entries, ticket_text) + contact_ok(reply, entries, ticket_text))
