"""Hybrid retrieval: lexical and dense results fused by reciprocal rank.

Lexical search is precise for exact terms (a product name, a menu label, an error code) and blind to paraphrase; dense
search is the opposite. Reciprocal-rank fusion combines them without needing their scores to be comparable: a document's
fused score is the sum over lists of 1 / (k0 + rank). A document both lists like rises; one only a single list likes still
gets in. k0 = 60 is the usual constant.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .dense import HashingEmbedder, ShardedDense
from .lexical import ShardedLexical
from .types import Hit

RRF_K0 = 60


def rrf(rankings: list[list[Hit]], k0: int = RRF_K0) -> list[Hit]:
    """Fuse ranked lists. Ties break by smaller id so the result is deterministic."""
    fused: dict[int, float] = {}
    seen_in: dict[int, list[str]] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            fused[hit.doc_id] = fused.get(hit.doc_id, 0.0) + 1.0 / (k0 + rank)
            seen_in.setdefault(hit.doc_id, []).append(hit.source)
    ordered = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
    return [Hit(i, s, "hybrid", {"in": seen_in[i]}) for i, s in ordered]


class HybridRetriever:
    def __init__(self, lexical: ShardedLexical, dense: ShardedDense, embedder: HashingEmbedder,
                 candidates: int = 100, nprobe: int = 8):
        self.lexical, self.dense, self.embedder = lexical, dense, embedder
        self.candidates, self.nprobe = candidates, nprobe
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hybrid")

    def search(self, text: str, k: int = 24, filters: dict | None = None) -> list[Hit]:
        """The `k` best entries for a query. Both indexes are searched in parallel for a wider candidate list, fused, then cut."""
        qvec = self.embedder.embed([text])[0]
        lex = self._pool.submit(self.lexical.search, text, self.candidates, filters)
        den = self._pool.submit(self.dense.search, qvec, self.candidates, self.nprobe)
        lexical_hits, dense_hits = lex.result(), den.result()
        if filters and any(filters.values()):  # the dense index carries no metadata: filter its hits through the lexical store
            ok = self.lexical.passing([h.doc_id for h in dense_hits], filters)
            dense_hits = [h for h in dense_hits if h.doc_id in ok]
        return rrf([lexical_hits, dense_hits])[:k]
