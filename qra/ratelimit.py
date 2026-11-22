"""Rate limiting with an injectable clock so tests never sleep for real."""
from __future__ import annotations

import time
from collections import deque
from typing import Callable


class RequestLimiter:
    """Spaces calls so no more than `rate` per second leave the process (client-side 429 avoidance)."""

    def __init__(self, rate: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval = 1.0 / rate
        self._clock, self._sleep = clock, sleep
        self._next = 0.0

    def wait(self) -> None:
        now = self._clock()
        if now < self._next:
            self._sleep(self._next - now)
            now = self._next
        self._next = now + self.interval


class TokenBudget:
    """Sliding one-minute window over model tokens, fed from each call's reported usage."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.per_minute = per_minute
        self._clock, self._sleep = clock, sleep
        self._events: deque[tuple[float, int]] = deque()

    def _trim(self, now: float) -> None:
        while self._events and now - self._events[0][0] >= 60.0:
            self._events.popleft()

    def used(self) -> int:
        self._trim(self._clock())
        return sum(t for _, t in self._events)

    def reserve(self, estimate: int) -> None:
        """Block until `estimate` tokens fit in the window."""
        while True:
            now = self._clock()
            self._trim(now)
            if not self._events or sum(t for _, t in self._events) + estimate <= self.per_minute:
                return
            self._sleep(max(0.05, 60.0 - (now - self._events[0][0])))

    def record(self, tokens: int) -> None:
        self._events.append((self._clock(), tokens))
