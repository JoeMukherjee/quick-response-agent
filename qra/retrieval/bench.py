"""Benchmarks for the retrieval methods.

compare(): the methods on a labelled query set (eval/retrieval_gold.jsonl). For each method: how many of the entries a
question needs were in the candidate set (recall), in what share of questions ALL needed entries were present (full
recall, the number that matters when an answer spans two entries), how many entries the judge would have to score, and
the retrieval latency.

scale(): the cascade's cost against corpus size, on a synthetic corpus, to show it stays sublinear.
"""
from __future__ import annotations

import json
import statistics
import tempfile
import time
from pathlib import Path

import numpy as np

from qra.core.faq import Faq, load_faq
from qra.core.models import Ticket

from .dense import HashingEmbedder, ShardedDense
from .hybrid import HybridRetriever
from .ingest import ingest
from .lexical import ShardedLexical
from .shortlist import Shortlister
from .types import Doc

GOLD = Path(__file__).resolve().parent.parent.parent / "eval" / "retrieval_gold.jsonl"


def _ticket(row: dict) -> Ticket:
    return Ticket(row["id"], "", row["query"], "email", row["plan"])


def compare(faq: Faq | None = None, k: int = 6, repeats: int = 200) -> dict:
    faq = faq or load_faq()
    rows = [json.loads(l) for l in GOLD.read_text(encoding="utf-8").splitlines() if l.strip()]
    tmp = tempfile.mkdtemp()
    cascade = Shortlister.from_faq(faq, Path(tmp), max_entries=k)

    methods = {
        "exhaustive (original)": lambda t: faq.ids(),
        f"cascade (top {k})": lambda t: cascade.for_ticket(t, faq).ids,
    }
    report = {}
    for name, fn in methods.items():
        found = need = full = size = 0
        for r in rows:
            got = set(fn(_ticket(r)))
            hit = len(got & set(r["gold"]))
            found, need, size = found + hit, need + len(r["gold"]), size + len(got)
            full += hit == len(r["gold"])
        t0 = time.perf_counter()
        for _ in range(repeats):
            for r in rows:
                fn(_ticket(r))
        per_query_us = (time.perf_counter() - t0) / (repeats * len(rows)) * 1e6
        report[name] = {"entry_recall": round(found / need, 3), "full_recall": f"{full}/{len(rows)}",
                        "avg_candidates": round(size / len(rows), 1), "latency_us": round(per_query_us, 1)}
    return {"queries": len(rows), "k": k, "methods": report}


def _synthetic(n: int, topics: int = 400, seed: int = 1):
    rng = np.random.default_rng(seed)
    vocab = [f"w{i}" for i in range(20000)]
    zipf = 1.0 / np.arange(1, len(vocab) + 1)
    zipf /= zipf.sum()
    topic_words = [rng.choice(len(vocab), size=40, replace=False) for _ in range(topics)]
    for i in range(n):
        topic = topic_words[i % topics]
        words = list(rng.choice(topic, size=14)) + list(rng.choice(len(vocab), size=10, p=zipf))
        yield Doc(i + 1, " ".join(vocab[w] for w in words) + f" u{i}")


def scale(docs: int = 200_000, shards: int = 8, queries: int = 200) -> dict:
    tmp = Path(tempfile.mkdtemp())
    emb = HashingEmbedder()
    lex, dense = ShardedLexical(tmp, shards), ShardedDense(shards, emb.dim, nlist=64)
    stats = ingest(_synthetic(docs), lex, dense, emb, batch_size=10_000, train_size=50_000)
    hybrid = HybridRetriever(lex, dense, emb, candidates=100, nprobe=8)
    rng = np.random.default_rng(7)
    sample = rng.choice(docs, size=queries, replace=False)
    docs_by_id = {int(i): d for i, d in zip(sample, (d for d in _synthetic(docs) if (d.id - 1) in set(map(int, sample))))}
    lat_lex, lat_den, lat_hyb, found_lex, found_hyb, ivf_hit, ivf_total = [], [], [], 0, 0, 0, 0
    for i in sample:
        doc = docs_by_id[int(i)]
        query = " ".join(doc.text.split()[:6]) + f" u{int(i)}"
        t = time.perf_counter(); h = lex.search(query, 10); lat_lex.append(time.perf_counter() - t)
        found_lex += any(x.doc_id == doc.id for x in h)
        qv = emb.embed([query])[0]
        t = time.perf_counter(); approx = dense.search(qv, 10, 8); lat_den.append(time.perf_counter() - t)
        exact = dense.exact(qv, 10)
        ivf_hit += len({x.doc_id for x in approx} & {x.doc_id for x in exact}); ivf_total += 10
        t = time.perf_counter(); hy = hybrid.search(query, 10); lat_hyb.append(time.perf_counter() - t)
        found_hyb += any(x.doc_id == doc.id for x in hy)
    ms = lambda xs, q: round(float(np.percentile(xs, q)) * 1000, 2)  # noqa: E731
    return {"docs": docs, "shards": shards, "ingest": stats,
            "lexical_ms": {"p50": ms(lat_lex, 50), "p95": ms(lat_lex, 95)},
            "dense_ms": {"p50": ms(lat_den, 50), "p95": ms(lat_den, 95)},
            "hybrid_ms": {"p50": ms(lat_hyb, 50), "p95": ms(lat_hyb, 95)},
            "lexical_finds_source_doc": f"{found_lex}/{queries}", "hybrid_finds_source_doc": f"{found_hyb}/{queries}",
            "ivf_recall_vs_exact_at_10": round(ivf_hit / ivf_total, 3),
            "disk_mb": round(sum(p.stat().st_size for p in tmp.glob("*.db")) / 1e6, 1)}
