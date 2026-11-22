"""Payment-data redaction. Runs on ticket text BEFORE any model call and before anything is logged,
and again on every outgoing reply and handoff note (defence in depth: the client rule says never repeat
card numbers or IBANs in a reply or a log).

Deliberately over-eager: redacting a harmless number costs a placeholder, leaking a card costs the client.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

IBAN_PLACEHOLDER = "[IBAN removed]"
CARD_PLACEHOLDER = "[card number removed]"

# Two letters + two check digits + 11..30 more alphanumerics, with optional single spaces/dots/dashes between groups.
_IBAN = re.compile(r"\b[A-Za-z]{2}\d{2}(?:[ .\-]?[A-Za-z0-9]){11,30}\b")
# 13..19 digits, optionally separated by single spaces or dashes.
_CARD = re.compile(r"(?<![\d])(?:\d[ \-]?){12,18}\d(?![\d])")

_COUNTRIES = {
    "AD", "AE", "AL", "AT", "AZ", "BA", "BE", "BG", "BH", "BR", "BY", "CH", "CR", "CY", "CZ", "DE", "DK", "DO",
    "EE", "EG", "ES", "FI", "FO", "FR", "GB", "GE", "GI", "GL", "GR", "GT", "HR", "HU", "IE", "IL", "IQ", "IS",
    "IT", "JO", "KW", "KZ", "LB", "LI", "LT", "LU", "LV", "MC", "MD", "ME", "MK", "MR", "MT", "MU", "NL", "NO",
    "PK", "PL", "PS", "PT", "QA", "RO", "RS", "SA", "SE", "SI", "SK", "SM", "TN", "TR", "UA", "VA", "VG", "XK",
}


@dataclass
class Redaction:
    text: str
    kinds: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.kinds)


def _luhn(digits: str) -> bool:
    total, flip = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if flip:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        flip = not flip
    return total % 10 == 0


def _is_card(match: str) -> bool:
    digits = re.sub(r"\D", "", match)
    if not 13 <= len(digits) <= 19:
        return False
    return _luhn(digits) or len(digits) >= 15  # long digit runs are treated as cards even without a valid check digit


def redact(text: str) -> Redaction:
    kinds: list[str] = []

    def iban_sub(m: re.Match) -> str:
        raw = m.group(0)
        if raw[:2].upper() in _COUNTRIES and re.search(r"\d", raw[4:]):
            kinds.append("iban")
            return IBAN_PLACEHOLDER
        return raw

    def card_sub(m: re.Match) -> str:
        if _is_card(m.group(0)):
            kinds.append("card")
            return CARD_PLACEHOLDER
        return m.group(0)

    out = _IBAN.sub(iban_sub, text)
    out = _CARD.sub(card_sub, out)
    return Redaction(out, kinds)


def contains_payment_data(text: str) -> bool:
    return redact(text).found
