"""Streaming, idempotent ingestion into the lexical and dense indexes.

Documents arrive as an iterator of any length and are processed in fixed-size batches, so memory is bounded by the batch
and not by the corpus. The lexical side is idempotent (the doc id is the row id, so re-ingesting replaces a document).
The dense side trains its coarse quantiser on the first `train_size` documents, then assigns every later vector to its
nearest cell. At very large scale the quantiser is retrained offline on a sample and the index rebuilt in the background
(a new generation swapped in), because re-assigning existing vectors requires a pass over them.
"""
from __future__ import annotations

import time
from itertools import islice
from typing import Iterable, Iterator

import numpy as np

from .dense import HashingEmbedder, ShardedDense
from .lexical import ShardedLexical
from .types import Doc


def _batches(docs: Iterable[Doc], size: int) -> Iterator[list[Doc]]:
    it = iter(docs)
    while batch := list(islice(it, size)):
        yield batch


def ingest(docs: Iterable[Doc], lexical: ShardedLexical, dense: ShardedDense | None, embedder: HashingEmbedder | None,
           batch_size: int = 5000, train_size: int = 20000, progress=None) -> dict:
    """Index every document. Returns counts and timings."""
    t0 = time.perf_counter()
    count, trained = 0, dense is None
    pending: list[Doc] = []  # documents buffered until there are enough to train the quantiser
    lex_s = den_s = 0.0

    def push(batch: list[Doc]) -> None:
        nonlocal lex_s, den_s
        t = time.perf_counter()
        lexical.upsert(batch)
        lex_s += time.perf_counter() - t
        if dense is not None:
            t = time.perf_counter()
            dense.add(np.array([d.id for d in batch], dtype=np.int64), embedder.embed([d.text for d in batch]))
            den_s += time.perf_counter() - t

    for batch in _batches(docs, batch_size):
        if not trained:
            pending.extend(batch)
            if len(pending) < train_size:
                continue
            dense.train(embedder.embed([d.text for d in pending[:train_size]]))
            trained = True
            for chunk in _batches(pending, batch_size):
                push(chunk)
            count += len(pending)
            pending = []
        else:
            push(batch)
            count += len(batch)
        if progress:
            progress(count)
    if pending:  # the corpus ended before there were `train_size` documents: train on what there is
        dense.train(embedder.embed([d.text for d in pending]))
        for chunk in _batches(pending, batch_size):
            push(chunk)
        count += len(pending)
    return {"docs": count, "seconds": round(time.perf_counter() - t0, 2), "lexical_s": round(lex_s, 2),
            "dense_s": round(den_s, 2), "docs_per_s": round(count / max(1e-9, time.perf_counter() - t0))}
