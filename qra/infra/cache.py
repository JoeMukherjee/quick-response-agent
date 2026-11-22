"""Persistent key-value cache for Decision responses (SQLite, WAL, indexed by primary key).

Keyed on sha256(model, input, questions): re-running the eval after changing thresholds or the truth table
replays free, and only reworded questions cost tokens. Safe to share between worker threads.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path


class DecisionCache:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS decisions (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def get(self, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM decisions WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO decisions VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))

    def __len__(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
