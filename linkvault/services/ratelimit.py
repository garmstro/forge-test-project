"""Rate limiting service using sliding window counter algorithm.

This module provides in-memory rate limiting with per-key tracking.
For distributed deployments, Redis support can be added in a future phase.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from threading import Lock


@dataclass
class RateLimitConfig:
    """Configuration for a rate limit rule."""

    max_requests: int
    window_seconds: int


class RateLimiter:
    """In-memory sliding window rate limiter.

    Tracks request timestamps per key and enforces limits based on
    a sliding window algorithm.
    """

    def __init__(self) -> None:
        """Initialize the rate limiter."""
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def is_allowed(self, key: str, config: RateLimitConfig) -> bool:
        """Check if a request is allowed under the given rate limit config.

        Args:
            key: Unique identifier (e.g., user_id, IP address).
            config: Rate limit configuration (max_requests, window_seconds).

        Returns:
            True if the request is allowed, False if rate limit is exceeded.
        """
        now = time.time()

        with self._lock:
            # Get or create request list for this key
            requests = self._requests[key]

            # Remove requests outside the sliding window
            cutoff = now - config.window_seconds
            requests[:] = [ts for ts in requests if ts > cutoff]

            # Check if we're under the limit
            if len(requests) < config.max_requests:
                requests.append(now)
                return True

            return False

    def get_remaining(self, key: str, config: RateLimitConfig) -> int:
        """Get the number of remaining requests for a key.

        Args:
            key: Unique identifier.
            config: Rate limit configuration.

        Returns:
            Number of requests remaining in the current window.
        """
        now = time.time()

        with self._lock:
            requests = self._requests[key]
            cutoff = now - config.window_seconds
            valid_requests = [ts for ts in requests if ts > cutoff]
            return max(0, config.max_requests - len(valid_requests))

    def get_reset_time(self, key: str, config: RateLimitConfig) -> float:
        """Get the Unix timestamp when the rate limit resets.

        Args:
            key: Unique identifier.
            config: Rate limit configuration.

        Returns:
            Unix timestamp of when the oldest request in the window expires.
        """
        now = time.time()

        with self._lock:
            requests = self._requests[key]
            cutoff = now - config.window_seconds
            valid_requests = [ts for ts in requests if ts > cutoff]

            if not valid_requests:
                return now

            # The oldest request in the window determines the reset time
            oldest = min(valid_requests)
            return oldest + config.window_seconds

    def reset(self, key: str | None = None) -> None:
        """Reset rate limit state.

        Args:
            key: If provided, reset only this key. If None, reset all keys.
        """
        with self._lock:
            if key is None:
                self._requests.clear()
            else:
                self._requests.pop(key, None)


# Global rate limiter instance
_global_limiter = RateLimiter()


def get_limiter() -> RateLimiter:
    """Get the global rate limiter instance."""
    return _global_limiter

