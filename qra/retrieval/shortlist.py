"""Chooses which knowledge-base entries the judge is asked about.

`exhaustive` (the original method): every entry is scored by the judge. Perfect recall, cost proportional to the size of
the knowledge base.

`cascade`: the sharded hybrid index picks the `max_entries` most promising entries for this ticket and only those reach
the judge. Cost is constant in the size of the knowledge base. If the right entry is not in the shortlist, the judge
finds no matching entry and the ticket is handed to a human, so a recall miss is a safe failure.

A knowledge base no larger than `max_entries` is always passed whole: there is nothing to cut.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from qra.core.faq import Faq
from qra.core.models import Ticket
from qra.safety.redact import redact

from .dense import HashingEmbedder, ShardedDense
from .hybrid import HybridRetriever
from .ingest import ingest
from .lexical import ShardedLexical
from .types import Doc

ID_BITS = (1 << 62) - 1


def doc_id(entry_id: str) -> int:
    """A stable 62-bit integer for a help-centre entry id."""
    return int.from_bytes(hashlib.blake2b(entry_id.encode("utf-8"), digest_size=8).digest(), "little") & ID_BITS


@dataclass(frozen=True)
class Shortlist:
    faq: Faq                 # the entries the judge will be asked about
    ids: list[str]           # their ids, best first
    exhaustive: bool         # True when nothing was cut


class Shortlister:
    def __init__(self, retriever: HybridRetriever | None, ids: dict[int, str], max_entries: int = 24):
        self.retriever, self._ids, self.max_entries = retriever, ids, max_entries

    @classmethod
    def from_faq(cls, faq: Faq, directory: Path, max_entries: int = 6, shards: int = 2) -> "Shortlister":
        """Index a (small) help centre in memory-sized shards. Re-running is safe: ingestion is idempotent."""
        embedder = HashingEmbedder()
        lexical = ShardedLexical(directory, shards=shards)
        dense = ShardedDense(shards, embedder.dim, nlist=max(1, min(4, len(faq.entries) // 3)))
        docs = [Doc(doc_id(e.id), f"{e.title}. {e.text}") for e in faq.entries.values()]
        ingest(docs, lexical, dense, embedder, batch_size=64, train_size=max(1, len(docs)))
        return cls(HybridRetriever(lexical, dense, embedder, candidates=max(2 * max_entries, 24), nprobe=4),
                   {doc_id(i): i for i in faq.entries}, max_entries)

    def for_ticket(self, ticket: Ticket, faq: Faq) -> Shortlist:
        if self.retriever is None or len(faq.entries) <= self.max_entries:
            return Shortlist(faq, faq.ids(), True)
        query = f"{redact(ticket.subject).text} {redact(ticket.body).text}"  # payment data never reaches an index query
        hits = self.retriever.search(query, k=self.max_entries)
        chosen = [self._ids[h.doc_id] for h in hits if h.doc_id in self._ids]
        if not chosen:  # nothing matched at all: the judge still gets a bounded, stable set rather than nothing
            chosen = faq.ids()[: self.max_entries]
        return Shortlist(faq.subset(chosen), chosen, False)
