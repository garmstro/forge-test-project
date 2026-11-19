"""In-process sliding-window rate limiter.

The limiter keeps, per key (typically the client IP), the timestamps of the
requests seen within the last ``window_seconds``.  A request is allowed when
fewer than ``limit`` timestamps remain in the window (a "sliding log").

Limitations: state lives in process memory.  It is not shared between worker
processes or hosts and is lost on restart — see DECISIONS.md §5.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Outcome of a single :meth:`SlidingWindowRateLimiter.hit` call."""

    allowed: bool
    remaining: int
    retry_after: int  # whole seconds until a slot frees up; 0 when allowed


class RateLimitExceeded(Exception):
    """Raised by API dependencies when a client exceeds its limit."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Rate limit exceeded; retry after {retry_after} s.")
        self.retry_after = retry_after


class SlidingWindowRateLimiter:
    """Thread-safe sliding-window limiter keyed by string.

    Args:
        window_seconds: Length of the sliding window.
        clock: Monotonic time source in seconds (injectable for tests).
        prune_interval_seconds: How often keys with no hits inside the window
            are dropped, so memory stays bounded by the number of *active* keys.
    """

    def __init__(
        self,
        window_seconds: float = 60.0,
        clock: Clock = time.monotonic,
        prune_interval_seconds: float = 60.0,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.window_seconds = window_seconds
        self._clock = clock
        self._prune_interval = prune_interval_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._last_prune = clock()

    def hit(self, key: str, limit: int) -> RateLimitDecision:
        """Record a request for *key* and decide whether it is allowed.

        Rejected requests are not recorded, so a client that keeps retrying
        regains access as soon as its oldest accepted request leaves the window.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self._lock:
            now = self._clock()
            if now - self._last_prune >= self._prune_interval:
                self._prune_locked(now)

            cutoff = now - self.window_seconds
            hits = self._hits.get(key)
            if hits is None:
                hits = deque()
                self._hits[key] = hits
            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= limit:
                wait = hits[0] + self.window_seconds - now
                return RateLimitDecision(
                    allowed=False,
                    remaining=0,
                    retry_after=max(1, math.ceil(wait)),
                )

            hits.append(now)
            return RateLimitDecision(
                allowed=True, remaining=limit - len(hits), retry_after=0
            )

    def prune(self) -> None:
        """Drop keys with no hits inside the current window."""
        with self._lock:
            self._prune_locked(self._clock())

    def reset(self) -> None:
        """Forget all state (used by tests)."""
        with self._lock:
            self._hits.clear()
            self._last_prune = self._clock()

    def __len__(self) -> int:
        """Number of keys currently tracked."""
        return len(self._hits)

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self.window_seconds
        stale = [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for k in stale:
            del self._hits[k]
        self._last_prune = now


# Process-wide limiters: one bucket for redirects, one shared by the
# unauthenticated /users endpoints (register + token).
redirect_limiter = SlidingWindowRateLimiter(window_seconds=60.0)
api_limiter = SlidingWindowRateLimiter(window_seconds=60.0)


def reset_all() -> None:
    """Reset every process-wide limiter."""
    redirect_limiter.reset()
    api_limiter.reset()
