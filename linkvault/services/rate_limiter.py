"""In-memory sliding-window rate limiter.

State is per process. ``check`` contains no ``await`` so it runs atomically
with respect to the asyncio event loop.
"""
from __future__ import annotations

import math
import time
from collections import deque

_PRUNE_EVERY = 1000
_STALE_AFTER_SECONDS = 3600.0


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._calls = 0

    def check(self, key: str, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
        """Record a hit for *key* and return ``(allowed, retry_after_seconds)``.

        Rejected requests are not recorded. ``retry_after_seconds`` is 0 when allowed.
        """
        now = time.monotonic()

        self._calls += 1
        if self._calls >= _PRUNE_EVERY:
            self._calls = 0
            self._prune(now, max(float(window_seconds), _STALE_AFTER_SECONDS))

        hits = self._hits.get(key)
        if hits is None:
            hits = deque()
            self._hits[key] = hits

        cutoff = now - window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= limit:
            if not hits:  # limit <= 0: nothing ever allowed
                return False, window_seconds
            retry_after = math.ceil(hits[0] + window_seconds - now)
            return False, max(1, retry_after)

        hits.append(now)
        return True, 0

    def _prune(self, now: float, max_age: float) -> None:
        """Drop keys that are empty or whose newest hit is older than *max_age*."""
        for key in [k for k, d in self._hits.items() if not d or d[-1] <= now - max_age]:
            del self._hits[key]

    def reset(self) -> None:
        self._hits.clear()
        self._calls = 0


limiter = SlidingWindowLimiter()
