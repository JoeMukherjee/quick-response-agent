"""Loads the help centre from data/faq.md at runtime, so adding an entry needs no code change."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_FAQ = Path(__file__).resolve().parent.parent.parent / "data" / "faq.md"
_ENTRY = re.compile(r"^## (F\d{2}) · (.+)$", re.M)


@dataclass(frozen=True)
class FaqEntry:
    id: str
    title: str
    text: str


@dataclass(frozen=True)
class Faq:
    entries: dict[str, FaqEntry]
    rules: str

    def __getitem__(self, entry_id: str) -> FaqEntry:
        return self.entries[entry_id]

    @property
    def digest(self) -> str:
        """Identifies this version of the help centre (used to memoise the question set built from it)."""
        import hashlib
        joined = chr(10).join(f"{e.id}:{e.text}" for e in self.entries.values())
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]

    def ids(self) -> list[str]:
        return list(self.entries)

    def subset(self, entry_ids: list[str]) -> "Faq":
        """The same help centre restricted to some entries, keeping the order given (best first)."""
        return Faq({i: self.entries[i] for i in entry_ids if i in self.entries}, self.rules)


def load_faq(path: Path = DEFAULT_FAQ) -> Faq:
    raw = Path(path).read_text(encoding="utf-8")
    rules_at = raw.find("## The client's rules")
    body, rules = (raw[:rules_at], raw[rules_at:]) if rules_at != -1 else (raw, "")
    matches = list(_ENTRY.finditer(body))
    entries: dict[str, FaqEntry] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        text = body[m.end():end].replace("---", "").strip()
        entries[m.group(1)] = FaqEntry(m.group(1), m.group(2).strip(), text)
    if not entries:
        raise ValueError(f"no FAQ entries found in {path}")
    return Faq(entries, rules.strip())
