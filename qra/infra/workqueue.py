"""Work-queue seam. The runner only talks to this tiny interface, so the in-process queue used here can be
replaced by Redis Streams, SQS or Pub/Sub for horizontal workers without touching pipeline or delivery code
(see docs/SCALING.md). Delivery stays exactly-once-per-ticket because the ledger, not the queue, owns that guarantee.
"""
from __future__ import annotations

import queue
from typing import Generic, Protocol, TypeVar

T = TypeVar("T")


class WorkQueue(Protocol[T]):
    def put(self, item: T) -> None: ...
    def get(self) -> T | None: ...  # None is the stop signal for one worker
    def close(self, workers: int) -> None: ...


class LocalQueue(Generic[T]):
    """Bounded in-process queue (back-pressure for the producer)."""

    def __init__(self, maxsize: int = 100):
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)

    def put(self, item: T) -> None:
        self._q.put(item)

    def get(self) -> T | None:
        return self._q.get()

    def close(self, workers: int) -> None:
        for _ in range(workers):
            self._q.put(None)
