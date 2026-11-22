"""Crash-recovery ledger on SQLite (stdlib, WAL, synchronous=FULL): atomic, indexed and safe across threads.

Transitions per ticket, in order:  decided -> sent | reconciled
  decided     the full payload is durable BEFORE any POST, so a crash never regenerates an answer
  sent        the helpdesk accepted our POST (2xx)
  reconciled  the helpdesk already had the ticket handled (409, or found handled by GET); we did not send

UNIQUE(epoch, ticket, state) makes the database itself refuse a second `decided` for a ticket, so two workers
(or two processes) can never both own the same decision. `epoch` changes on every practice reset.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

TERMINAL = {"sent", "reconciled"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transitions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    epoch   TEXT NOT NULL,
    ticket  TEXT NOT NULL,
    state   TEXT NOT NULL CHECK (state IN ('decided','sent','reconciled')),
    action  TEXT,
    payload TEXT,
    why     TEXT,
    ts      TEXT NOT NULL,
    UNIQUE (epoch, ticket, state)
);
CREATE INDEX IF NOT EXISTS idx_transitions_ticket ON transitions (epoch, ticket, id);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        row = self._db.execute("SELECT v FROM meta WHERE k='epoch'").fetchone()
        if row is None:
            self._db.execute("INSERT INTO meta VALUES ('epoch', '1')")
        self.epoch = row[0] if row else "1"

    def new_epoch(self) -> str:
        """Called after /practice/reset: tickets are open again, so earlier `sent` records must not skip them."""
        with self._lock:
            self.epoch = str(int(self.epoch) + 1)
            self._db.execute("UPDATE meta SET v=? WHERE k='epoch'", (self.epoch,))
        return self.epoch

    def _insert(self, ticket_id: str, state: str, action: str | None = None, payload: dict | None = None,
                why: str | None = None) -> bool:
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO transitions (epoch, ticket, state, action, payload, why, ts) VALUES (?,?,?,?,?,?,?)",
                    (self.epoch, ticket_id, state, action,
                     json.dumps(payload, ensure_ascii=False) if payload is not None else None, why,
                     time.strftime("%Y-%m-%dT%H:%M:%S%z")))
                return True
            except sqlite3.IntegrityError:
                return False  # this ticket already has this transition in this epoch

    def state(self, ticket_id: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT state FROM transitions WHERE epoch=? AND ticket=? ORDER BY id DESC LIMIT 1",
                (self.epoch, ticket_id)).fetchone()
        return row[0] if row else None

    def decision(self, ticket_id: str) -> tuple[str, dict] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT action, payload FROM transitions WHERE epoch=? AND ticket=? AND state='decided'",
                (self.epoch, ticket_id)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def record_decided(self, ticket_id: str, action: str, payload: dict) -> bool:
        """True if this call created the decision; False if one already existed (it is never overwritten)."""
        return self._insert(ticket_id, "decided", action, payload)

    def record_sent(self, ticket_id: str) -> None:
        self._insert(ticket_id, "sent")

    def record_reconciled(self, ticket_id: str, why: str) -> None:
        self._insert(ticket_id, "reconciled", why=why)

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT state, COUNT(*) FROM transitions WHERE epoch=? GROUP BY state", (self.epoch,)).fetchall()
        return dict(rows)

    def clear(self) -> None:
        self.new_epoch()

    def close(self) -> None:
        self._db.close()
