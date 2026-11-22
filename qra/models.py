"""Plain data classes shared across modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Action(str, Enum):
    REPLY = "reply"
    HANDOFF = "handoff"
    CLOSE = "close"


@dataclass(frozen=True)
class Ticket:
    id: str
    subject: str
    body: str
    channel: str = ""
    customer_plan: str = ""

    @classmethod
    def from_api(cls, raw: dict) -> "Ticket":
        return cls(raw["id"], raw.get("subject") or "", raw.get("body") or "",
                   raw.get("channel") or "", raw.get("customer_plan") or "")


@dataclass(frozen=True)
class Answers:
    """Typed view over one Decision response: yes-probabilities and choice distributions."""
    noul: dict[str, float] = field(default_factory=dict)
    choice: dict[str, dict[str, float]] = field(default_factory=dict)
    tokens: int = 0

    def p(self, key: str) -> float:
        return self.noul[key]

    def top(self, key: str) -> tuple[str, float]:
        probs = self.choice[key]
        best = max(probs, key=lambda k: (probs[k], k))
        return best, probs[best]
