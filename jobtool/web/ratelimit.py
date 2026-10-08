"""In-memory per-visitor sliding-window rate limiter (single process; good enough for the demo)."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, per_minute: int):
        self.limit = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if now - self._last_sweep > 300:
                self._sweep(now)
        return True

    def _sweep(self, now: float) -> None:
        for key in [k for k, v in self._hits.items() if not v or now - v[-1] > 60]:
            del self._hits[key]
        self._last_sweep = now
