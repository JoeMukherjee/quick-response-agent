"""Append-only crash-recovery ledger (JSONL, fsync per write).

States per ticket, in order: decided -> sent | reconciled.
  decided     the full payload is durable BEFORE any POST, so a crash never regenerates an answer
  sent        the helpdesk accepted our POST (2xx)
  reconciled  the helpdesk already had the ticket handled (409, or found handled by GET); we did not send
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

TERMINAL = {"sent", "reconciled"}


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # truncated last line from a crash mid-write; earlier lines are intact
                self._apply(rec)

    def _apply(self, rec: dict) -> None:
        cur = self._state.setdefault(rec["ticket"], {})
        cur["state"] = rec["state"]
        if "action" in rec:
            cur["action"], cur["payload"] = rec["action"], rec["payload"]

    def _append(self, rec: dict) -> None:
        rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._apply(rec)

    def state(self, ticket_id: str) -> str | None:
        return self._state.get(ticket_id, {}).get("state")

    def decision(self, ticket_id: str) -> tuple[str, dict] | None:
        cur = self._state.get(ticket_id)
        return (cur["action"], cur["payload"]) if cur and "action" in cur else None

    def record_decided(self, ticket_id: str, action: str, payload: dict) -> None:
        self._append({"ticket": ticket_id, "state": "decided", "action": action, "payload": payload})

    def record_sent(self, ticket_id: str) -> None:
        self._append({"ticket": ticket_id, "state": "sent"})

    def record_reconciled(self, ticket_id: str, why: str) -> None:
        self._append({"ticket": ticket_id, "state": "reconciled", "why": why})

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
        self._state.clear()
