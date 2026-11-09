"""In-process sliding-window limiter for creation endpoints (CONTRACTS.md section 3).

shortcut: state lives in one API process; correct for the single-process local MVP, replace
with shared state before running several API replicas.
"""

import threading
from collections import deque
from collections.abc import Callable


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_s: float, clock: Callable[[], float]) -> None:
        self._limit = limit
        self._window = window_s
        self._clock = clock
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def check(self) -> float | None:
        """Record a hit. Returns None if allowed, else seconds until a slot frees up."""
        now = self._clock()
        with self._lock:
            while self._hits and now - self._hits[0] >= self._window:
                self._hits.popleft()
            if len(self._hits) >= self._limit:
                return self._window - (now - self._hits[0])
            self._hits.append(now)
            return None
