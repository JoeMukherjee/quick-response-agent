"""One JSON log line per ticket in logs/. Never contains the ticket body or payment data."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .redact import redact

_FORBIDDEN_KEYS = {"body", "message", "authorization", "api_key", "key"}


class RunLog:
    def __init__(self, directory: Path, run_id: str):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"run-{run_id}.jsonl"
        self._lock = threading.Lock()

    def write(self, **fields) -> None:
        line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **{k: v for k, v in fields.items() if k not in _FORBIDDEN_KEYS}}
        text = json.dumps(line, ensure_ascii=False)
        text = redact(text).text  # final scrub: nothing that looks like an IBAN or a card number reaches disk
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
