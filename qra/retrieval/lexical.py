"""Lexical retrieval: BM25 over a sharded inverted index (SQLite FTS5, standard library).

Each shard is an FTS5 table. A query is routed to every shard in parallel and the per-shard top-k lists are merged
(scatter-gather). Two choices keep a query cheap however large the shard is:

  * Rare terms only. The query is cut down to its few most selective terms (lowest document frequency, read from the
    FTS5 vocabulary table), because the work of an OR query is the size of the union of the postings it touches, and
    common words are what make that union huge. Terms present in more than `max_df` of a shard are treated as stop words.
  * A bounded top-k. Each shard returns at most `k` rows ordered by BM25, never the full match set.

Scores are shard-local BM25 (each shard uses its own term statistics). With uniform hash sharding the shards are
statistically alike, which is the same trade the usual "query then fetch" search engines make.
"""
from __future__ import annotations

import heapq
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .text import match_query, tokens
from .types import Doc, Hit, shard_of


class LexicalShard:
    def __init__(self, path: Path | str):
        self._lock = threading.RLock()  # search() calls count() while it already holds the lock
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30.0)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(body, plan UNINDEXED, lang UNINDEXED, "
            "tokenize='unicode61 remove_diacritics 2')")
        self._db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vocab USING fts5vocab(docs, 'row')")

    def upsert(self, batch: list[Doc]) -> None:
        """Idempotent: the doc id is the row id, so re-ingesting a document replaces it."""
        with self._lock:
            self._db.execute("BEGIN")
            self._db.executemany("INSERT OR REPLACE INTO docs (rowid, body, plan, lang) VALUES (?,?,?,?)",
                                 [(d.id, d.text, d.plan, d.lang) for d in batch])
            self._db.execute("COMMIT")

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]

    def _selective_terms(self, words: list[str], max_terms: int, max_df: float) -> list[str]:
        n = self.count() or 1
        scored = []
        for w in dict.fromkeys(words):  # unique, in order
            row = self._db.execute("SELECT doc FROM vocab WHERE term=?", (w,)).fetchone()
            if row and row[0] <= max_df * n:
                scored.append((row[0], w))
        scored.sort()
        return [w for _, w in scored[:max_terms]]

    def search(self, query: str, k: int, filters: dict | None = None, max_terms: int = 8, max_df: float = 0.2) -> list[Hit]:
        with self._lock:
            terms = self._selective_terms(tokens(query), max_terms, max_df)
            if not terms:
                return []
            where, args = "docs MATCH ?", [match_query(terms)]
            for col in ("plan", "lang"):
                if filters and filters.get(col):
                    where += f" AND {col}=?"
                    args.append(filters[col])
            rows = self._db.execute(f"SELECT rowid, bm25(docs) FROM docs WHERE {where} ORDER BY bm25(docs) LIMIT ?",
                                    (*args, k)).fetchall()
        return [Hit(r, -s, "lexical") for r, s in rows]  # FTS5 reports BM25 as a negative number: lower is better

    def passing(self, ids: list[int], filters: dict) -> set[int]:
        """Which of these ids satisfy the metadata filters (used to filter dense hits)."""
        if not ids:
            return set()
        marks = ",".join("?" * len(ids))
        where, args = f"rowid IN ({marks})", list(ids)
        for col in ("plan", "lang"):
            if filters.get(col):
                where += f" AND {col}=?"
                args.append(filters[col])
        with self._lock:
            return {r[0] for r in self._db.execute(f"SELECT rowid FROM docs WHERE {where}", args)}

    def close(self) -> None:
        self._db.close()


class ShardedLexical:
    def __init__(self, directory: Path | str, shards: int = 4):
        self.directory, self.shards = Path(directory), shards
        self.directory.mkdir(parents=True, exist_ok=True)
        self._shards = [LexicalShard(self.directory / f"lex-{i:03d}.db") for i in range(shards)]
        self._pool = ThreadPoolExecutor(max_workers=shards, thread_name_prefix="lex")

    def upsert(self, batch: list[Doc]) -> None:
        by_shard: dict[int, list[Doc]] = {}
        for d in batch:
            by_shard.setdefault(shard_of(d.id, self.shards), []).append(d)
        list(self._pool.map(lambda kv: self._shards[kv[0]].upsert(kv[1]), by_shard.items()))

    def count(self) -> int:
        return sum(s.count() for s in self._shards)

    def search(self, query: str, k: int, filters: dict | None = None) -> list[Hit]:
        parts = self._pool.map(lambda s: s.search(query, k, filters), self._shards)
        return heapq.nlargest(k, (h for part in parts for h in part), key=lambda h: h.score)

    def passing(self, ids: list[int], filters: dict) -> set[int]:
        by_shard: dict[int, list[int]] = {}
        for i in ids:
            by_shard.setdefault(shard_of(i, self.shards), []).append(i)
        out: set[int] = set()
        for found in self._pool.map(lambda kv: self._shards[kv[0]].passing(kv[1], filters), by_shard.items()):
            out |= found
        return out

    def close(self) -> None:
        self._pool.shutdown(wait=False)
        for s in self._shards:
            s.close()
