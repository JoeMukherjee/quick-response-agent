"""Shared types and the id helpers every shard agrees on."""
from __future__ import annotations

from dataclasses import dataclass, field

MASK64 = (1 << 64) - 1
_GOLDEN = 0x9E3779B97F4A7C15  # Fibonacci hashing: spreads consecutive ids evenly over shards


@dataclass(frozen=True)
class Doc:
    """One knowledge-base entry. `id` is a non-negative 62-bit integer; the document store maps it back to the full text."""
    id: int
    text: str
    plan: str = ""   # example metadata filters: which plan an entry applies to ...
    lang: str = ""   # ... and its language


@dataclass(frozen=True)
class Hit:
    doc_id: int
    score: float
    source: str = ""  # "lexical", "dense" or "hybrid"
    detail: dict = field(default_factory=dict, compare=False)


def shard_of(doc_id: int, shards: int) -> int:
    """Stable shard for an id. Adding shards later means re-hashing with a larger count (or consistent hashing)."""
    return (((doc_id * _GOLDEN) & MASK64) >> 32) % shards
