"""Thread-safe rate limiting with an injectable clock, so tests never sleep for real."""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable


class RequestLimiter:
    """Spaces calls so no more than `rate` per second leave the process, however many threads call it.

    Each caller atomically claims the next free slot and sleeps OUTSIDE the lock until that slot arrives.
    """

    def __init__(self, rate: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval = 1.0 / rate
        self._clock, self._sleep = clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            slot = max(now, self._next)
            self._next = slot + self.interval
        if slot > now:
            self._sleep(slot - now)

    def penalise(self, seconds: float) -> None:
        """After a 429: push every caller's next slot out so the whole pool backs off together."""
        with self._lock:
            self._next = max(self._next, self._clock() + seconds)


class TokenBudget:
    """Sliding one-minute window over model tokens, fed from each call's reported usage."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.per_minute = per_minute
        self._clock, self._sleep = clock, sleep
        self._events: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def _trim(self, now: float) -> None:
        while self._events and now - self._events[0][0] >= 60.0:
            self._events.popleft()

    def used(self) -> int:
        with self._lock:
            self._trim(self._clock())
            return sum(t for _, t in self._events)

    def reserve(self, estimate: int) -> None:
        """Block until `estimate` tokens fit in the window, then count them as spent (corrected by `record`)."""
        while True:
            with self._lock:
                now = self._clock()
                self._trim(now)
                if not self._events or sum(t for _, t in self._events) + estimate <= self.per_minute:
                    self._events.append((now, estimate))
                    return
                wait = max(0.05, 60.0 - (now - self._events[0][0]))
            self._sleep(wait)

    def record(self, tokens: int, estimate: int = 0) -> None:
        """Replace the reserved estimate with the real usage reported by the API."""
        with self._lock:
            for idx in range(len(self._events) - 1, -1, -1):
                ts, t = self._events[idx]
                if t == estimate:
                    self._events[idx] = (ts, tokens)
                    return
            self._events.append((self._clock(), tokens))
