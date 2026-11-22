"""One JSON log line per ticket in logs/. Never contains the ticket body or payment data."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .redact import redact

_FORBIDDEN_KEYS = {"body", "message", "authorization", "api_key", "key"}


def scrub(value: Any) -> Any:
    """Redact payment data in every string value and round floats; structure and numbers stay intact."""
    if isinstance(value, str):
        return redact(value).text
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in _FORBIDDEN_KEYS}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


class RunLog:
    def __init__(self, directory: Path, run_id: str):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"run-{run_id}.jsonl"
        self._lock = threading.Lock()

    def write(self, **fields) -> None:
        line = scrub({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **fields})
        text = json.dumps(line, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
