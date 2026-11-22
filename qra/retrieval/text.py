"""Tokenising and building safe full-text queries."""
from __future__ import annotations

import re
import unicodedata

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
STOP = frozenset("""a an the and or of to in on at for is are was be it this that with as by from you your we our
de het een en van voor op te dat die is zijn niet met aan als ook maar kan ik we jullie mijn onze er bij om
der die das und ein eine ist nicht mit auf""".split())


def fold(text: str) -> str:
    """Lowercase and strip accents so 'Wachtwoord' and 'wachtwóórd' meet."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def tokens(text: str) -> list[str]:
    return [t for t in _WORD.findall(fold(text)) if t not in STOP and len(t) > 1]


def match_query(terms: list[str]) -> str:
    """An FTS5 MATCH expression from plain terms. Every term is quoted and made of word characters only, so ticket text
    can never inject an FTS5 operator (AND, NEAR, *, column filters...)."""
    return " OR ".join(f'"{t}"' for t in terms if t.isalnum())
