"""Persistent, thread-safe response cache (SQLite, WAL, primary-key index) with hit/miss counters.

Used for model responses: Decision answers and Flash drafts/notes are keyed by a hash of everything that
determines them (model, instructions or questions, input), so a changed wording or model can never serve a stale
answer, and an identical repeat is free and instant. `namespace` keeps different kinds of entries apart.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path


def cache_key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


class KVCache:
    def __init__(self, path: Path, namespace: str = "decisions"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS kv (ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
                         "PRIMARY KEY (ns, key))")
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE ns=? AND key=?", (self.namespace, key)).fetchone()
            if row:
                self.hits += 1
            else:
                self.misses += 1
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?, ?)",
                             (self.namespace, key, json.dumps(value, ensure_ascii=False)))

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"namespace": self.namespace, "hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0, "entries": len(self)}

    def __len__(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM kv WHERE ns=?", (self.namespace,)).fetchone()[0]


DecisionCache = KVCache  # the Decision cache is the "decisions" namespace of the same store
