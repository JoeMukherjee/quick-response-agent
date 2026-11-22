"""Retrieval that scales with the knowledge base, so the model's cost does not.

The decision pipeline asks the judge one question per candidate help-centre entry. That is perfect recall and fine for
twelve entries, but its cost grows with the size of the knowledge base. This package puts a cascade in front of the
judge:

    query -> [sharded lexical index (BM25)]  \\
                                              >-- reciprocal-rank fusion --> shortlist (<= 24) --> judge ranks them
          -> [sharded dense index (IVF, int8)] /

Every stage before the judge is sublinear in the number of entries (an inverted index, and a coarse quantiser that
visits a few cells), so the judge always sees a bounded shortlist and its token cost is constant. If the right entry is
not in the shortlist the pipeline finds no matching entry and hands the ticket to a human: a recall miss is a safe
failure, never a wrong reply.

See docs/RETRIEVAL.md for the design, the capacity arithmetic for a billion entries, and the measured numbers.
"""
from .hybrid import HybridRetriever, rrf
from .ingest import ingest
from .shortlist import Shortlister
from .types import Doc, Hit

__all__ = ["Doc", "Hit", "HybridRetriever", "Shortlister", "ingest", "rrf"]
