"""Bounded, frequency-aware response cache (SQLite for persistence, a hash map for the hot path).

Goals, in order:
  * bounded size: a byte budget and an entry budget per namespace; the database can never grow past them;
  * keep what is useful: eviction removes the entries with the fewest hits first (least frequently used), oldest
    first among equals, and every eviction pass halves all hit counts so an entry that was popular last week
    cannot squat forever (the same idea as Redis' LFU decay);
  * fast lookups: a hit on a hot key is one hash-map read (O(1), no SQL). A cold key is one primary-key lookup in a
    WITHOUT ROWID table, so the row sits in the index itself (one B-tree descent, O(log n), no second hop);
  * cheap bookkeeping: hit counts are batched in memory and flushed in one transaction, so reading never writes
    once per hit.

The cache is a performance layer: a lookup that fails or an entry that was evicted only costs a recomputation.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import sqlite3
import threading
import time
import weakref
from pathlib import Path
from typing import Callable

MB = 1024 * 1024
ROW_OVERHEAD = 64  # bytes counted per row on top of key and value (index and page overhead, conservatively)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    ns        TEXT    NOT NULL,
    key       TEXT    NOT NULL,
    value     TEXT    NOT NULL,
    hits      INTEGER NOT NULL DEFAULT 0,
    last_used REAL    NOT NULL DEFAULT 0,
    size      INTEGER NOT NULL DEFAULT 0,
    tag       TEXT,
    PRIMARY KEY (ns, key)
) WITHOUT ROWID;
-- eviction order: the least used, then the oldest, come out of this index first without sorting the table
CREATE INDEX IF NOT EXISTS idx_kv_evict ON kv (ns, hits, last_used);
-- reverse index: tag -> keys, so "drop everything for the old model or help-centre version" is one indexed query
CREATE INDEX IF NOT EXISTS idx_kv_tag ON kv (ns, tag) WHERE tag IS NOT NULL;
"""


def cache_key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


_LIVE: "weakref.WeakSet[KVCache]" = weakref.WeakSet()  # every open cache, so a clear reaches the in-memory tier too


def _close_all() -> None:  # one exit hook for all caches (a per-instance hook would keep every cache alive forever)
    for cache in list(_LIVE):
        cache.close()


atexit.register(_close_all)


class KVCache:
    HOT_MAX = 256            # values kept in memory for O(1) hits
    FLUSH_EVERY = 64         # hit counters are written back in batches of this many distinct keys
    LOW_WATER = 0.9          # evict down to this fraction of a limit so we do not evict on every put
    DECAY_SECONDS = 3600     # hit counts are halved at most this often, so popularity fades with time, not with churn

    def __init__(self, path: Path, namespace: str = "decisions", max_bytes: int = 64 * MB, max_entries: int = 50_000,
                 clock: Callable[[], float] = time.time):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path).resolve()
        self.namespace, self.max_bytes, self.max_entries, self._clock = namespace, max_bytes, max_entries, clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        self._db.executescript(_SCHEMA)
        row = self._db.execute("SELECT COALESCE(SUM(size), 0), COUNT(*) FROM kv WHERE ns=?", (namespace,)).fetchone()
        self._bytes, self._entries = int(row[0]), int(row[1])
        self._hot: dict[str, list] = {}      # key -> [value, hits since flush]
        self._pending: dict[str, int] = {}   # key -> hits not yet written back
        self.hits = self.misses = self.evictions = 0
        self._last_decay = clock()
        self._closed = False
        _LIVE.add(self)

    # ---- schema upgrade (older stores had no hit counts and used a rowid table) --------------------------
    def _migrate(self) -> None:
        cols = [r[1] for r in self._db.execute("PRAGMA table_info(kv)")]
        if cols and "hits" in cols and "tag" not in cols:
            self._db.execute("ALTER TABLE kv ADD COLUMN tag TEXT")
        if cols and "hits" not in cols:
            self._db.execute("ALTER TABLE kv RENAME TO kv_old")
            self._db.executescript(_SCHEMA)
            self._db.execute("INSERT OR IGNORE INTO kv (ns, key, value, size) "
                             f"SELECT ns, key, value, LENGTH(value) + LENGTH(key) + {ROW_OVERHEAD} FROM kv_old")
            self._db.execute("DROP TABLE kv_old")

    # ---- reads -----------------------------------------------------------------------------------------
    def get(self, key: str) -> dict | None:
        with self._lock:
            slot = self._hot.get(key)
            if slot is not None:  # hot path: one hash-map read, no SQL
                self.hits += 1
                self._record_hit(key)
                return slot[0]
            row = self._db.execute("SELECT value FROM kv WHERE ns=? AND key=?", (self.namespace, key)).fetchone()
            if row is None:
                self.misses += 1
                return None
            value = json.loads(row[0])
            self.hits += 1
            self._admit(key, value)
            self._record_hit(key)
            return value

    def _record_hit(self, key: str) -> None:
        self._pending[key] = self._pending.get(key, 0) + 1
        if len(self._pending) >= self.FLUSH_EVERY:
            self.flush()

    def _admit(self, key: str, value: dict) -> None:
        if len(self._hot) >= self.HOT_MAX:  # drop the coldest hot entry (fewest recent hits)
            coldest = min(self._hot, key=lambda k: self._pending.get(k, 0))
            self._hot.pop(coldest, None)
        self._hot[key] = [value, 0]

    def flush(self) -> None:
        """Write batched hit counts back in one transaction."""
        with self._lock:
            if not self._pending:
                return
            now = self._clock()
            rows = [(n, now, self.namespace, k) for k, n in self._pending.items()]
            self._pending.clear()
            self._db.execute("BEGIN")
            self._db.executemany("UPDATE kv SET hits = hits + ?, last_used = ? WHERE ns=? AND key=?", rows)
            self._db.execute("COMMIT")

    # ---- writes ------------------------------------------------------------------------------------------
    def put(self, key: str, value: dict, tag: str | None = None) -> bool:
        blob = json.dumps(value, ensure_ascii=False)
        size = len(blob) + len(key) + ROW_OVERHEAD
        if size > self.max_bytes // 4:  # one entry must never be able to crowd out the rest
            return False
        with self._lock:
            old = self._db.execute("SELECT size FROM kv WHERE ns=? AND key=?", (self.namespace, key)).fetchone()
            self._db.execute(
                "INSERT INTO kv (ns, key, value, hits, last_used, size, tag) VALUES (?,?,?,0,?,?,?) "
                "ON CONFLICT(ns, key) DO UPDATE SET value=excluded.value, size=excluded.size, "
                "last_used=excluded.last_used, tag=excluded.tag",
                (self.namespace, key, blob, self._clock(), size, tag))
            if old is None:
                self._entries += 1
                self._bytes += size
            else:
                self._bytes += size - int(old[0])
            if key in self._hot:
                self._hot[key][0] = value
            if self._bytes > self.max_bytes or self._entries > self.max_entries:
                self._evict(protect=key)
            return True

    def _evict(self, protect: str) -> None:
        """Remove the least frequently used entries until both budgets are back under their low-water mark."""
        self.flush()
        now = self._clock()
        if now - self._last_decay >= self.DECAY_SECONDS:
            # decay: halve every hit count so old popularity fades and newer entries can earn their place
            self._db.execute("UPDATE kv SET hits = hits / 2 WHERE ns=?", (self.namespace,))
            self._last_decay = now
        target_bytes, target_entries = int(self.max_bytes * self.LOW_WATER), int(self.max_entries * self.LOW_WATER)
        while self._bytes > target_bytes or self._entries > target_entries:
            victims = self._db.execute(
                "SELECT key, size FROM kv WHERE ns=? AND key<>? ORDER BY hits ASC, last_used ASC LIMIT 32",
                (self.namespace, protect)).fetchall()
            if not victims:
                break
            self._db.execute("BEGIN")
            for key, size in victims:
                self._db.execute("DELETE FROM kv WHERE ns=? AND key=?", (self.namespace, key))
                self._hot.pop(key, None)
                self._bytes -= int(size)
                self._entries -= 1
                self.evictions += 1
                if self._bytes <= target_bytes and self._entries <= target_entries:
                    break
            self._db.execute("COMMIT")

    def invalidate(self, tag: str) -> int:
        """Drop every entry carrying `tag` (reverse index: tag -> keys). Returns how many were removed."""
        with self._lock:
            self.flush()
            rows = self._db.execute("SELECT key, size FROM kv WHERE ns=? AND tag=?", (self.namespace, tag)).fetchall()
            return self._delete(rows)

    def invalidate_except(self, keep_tags: set[str]) -> int:
        """Drop every tagged entry whose tag is not in `keep_tags` (for example after the model or help centre changed)."""
        with self._lock:
            self.flush()
            rows = [r for r in self._db.execute("SELECT key, size, tag FROM kv WHERE ns=? AND tag IS NOT NULL", (self.namespace,))
                    if r[2] not in keep_tags]
            return self._delete([(k, s) for k, s, _ in rows])

    def _delete(self, rows: list) -> int:
        if not rows:
            return 0
        self._db.execute("BEGIN")
        for key, size in rows:
            self._db.execute("DELETE FROM kv WHERE ns=? AND key=?", (self.namespace, key))
            self._hot.pop(key, None)
            self._pending.pop(key, None)
            self._bytes -= int(size)
            self._entries -= 1
        self._db.execute("COMMIT")
        return len(rows)

    def clear(self) -> int:
        """Remove every entry of this namespace, from the database and from memory. Returns how many were removed."""
        with self._lock:
            if self._closed:
                return 0
            removed = self._entries
            self._db.execute("DELETE FROM kv WHERE ns=?", (self.namespace,))
            self._hot.clear()
            self._pending.clear()
            self._bytes = self._entries = 0
            return removed

    def tags(self) -> dict[str, int]:
        with self._lock:
            return dict(self._db.execute(
                "SELECT tag, COUNT(*) FROM kv WHERE ns=? AND tag IS NOT NULL GROUP BY tag", (self.namespace,)).fetchall())

    # ---- introspection -----------------------------------------------------------------------------------
    def stats(self) -> dict:
        total = self.hits + self.misses
        with self._lock:
            return {"namespace": self.namespace, "hits": self.hits, "misses": self.misses,
                    "hit_rate": round(self.hits / total, 3) if total else 0.0, "entries": self._entries,
                    "bytes": self._bytes, "max_bytes": self.max_bytes, "max_entries": self.max_entries,
                    "evictions": self.evictions, "hot": len(self._hot)}

    def __len__(self) -> int:
        return self._entries

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _LIVE.discard(self)
        try:
            self.flush()
            self._db.close()
        except Exception:  # closing at interpreter exit must never raise
            pass


def store_stats(path: Path) -> dict[str, dict]:
    """Per-namespace size of a cache file, read straight from the database (works without opening a cache)."""
    path = Path(path)
    if not path.exists():
        return {}
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5.0)
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(kv)")]
        if not cols:
            return {}
        if "hits" in cols:
            rows = db.execute("SELECT ns, COUNT(*), COALESCE(SUM(size), 0), COALESCE(SUM(hits), 0) FROM kv GROUP BY ns").fetchall()
        else:  # a store written before hit counts existed: still report what is in it
            rows = db.execute("SELECT ns, COUNT(*), COALESCE(SUM(LENGTH(value)), 0), 0 FROM kv GROUP BY ns").fetchall()
        return {ns: {"entries": n, "bytes": b, "hits": h} for ns, n, b, h in rows}
    finally:
        db.close()


def clear_store(path: Path, namespace: str | None = None) -> dict[str, int]:
    """Clear one namespace (or all of them) in a cache file, including the memory tier of any open cache on it."""
    path = Path(path).resolve()
    removed: dict[str, int] = {}
    for cache in list(_LIVE):
        if cache.path == path and (namespace is None or cache.namespace == namespace):
            removed[cache.namespace] = removed.get(cache.namespace, 0) + cache.clear()
    if path.exists():
        db = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
        try:
            if [r for r in db.execute("PRAGMA table_info(kv)")]:  # the table exists (current or legacy layout)
                query, args = ("SELECT ns, COUNT(*) FROM kv" + (" WHERE ns=?" if namespace else "") + " GROUP BY ns", (namespace,) if namespace else ())
                for ns, n in db.execute(query, args).fetchall():  # rows no open cache accounted for
                    removed[ns] = removed.get(ns, 0) + n
                db.execute("DELETE FROM kv" + (" WHERE ns=?" if namespace else ""), args)
        finally:
            db.close()
    return removed


DecisionCache = KVCache  # the Decision cache is the "decisions" namespace of the same store
