"""
Rate limiting service for LinkVault.

Implements sliding window rate limiting with support for both user-based
(authenticated via API key) and IP-based (unauthenticated) limits.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""

    # Authenticated user limits (per minute)
    user_requests_per_minute: int = 60

    # Authenticated user limits (per hour)
    user_requests_per_hour: int = 1000

    # Unauthenticated IP limits (per minute)
    ip_requests_per_minute: int = 20

    # Unauthenticated IP limits (per hour)
    ip_requests_per_hour: int = 200

    # Enable/disable rate limiting
    enabled: bool = True


@dataclass
class RateLimitStatus:
    """Status of a rate limit check."""

    allowed: bool
    remaining_minute: int
    remaining_hour: int
    reset_minute_at: float
    reset_hour_at: float


class RateLimiter:
    """
    In-memory sliding window rate limiter.

    Tracks request counts per identifier (user ID or IP address) with
    separate windows for minute and hour limits.
    """

    def __init__(self, config: Optional[RateLimitConfig] = None):
        self.config = config or RateLimitConfig()
        # Structure: {identifier: [(timestamp, window_type), ...]}
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock_acquired_at: dict[str, float] = {}

    def check_limit(
        self,
        identifier: str,
        is_user: bool = False,
    ) -> RateLimitStatus:
        """
        Check if a request from the given identifier is allowed.

        Args:
            identifier: User ID or IP address
            is_user: True if this is an authenticated user, False for IP-based

        Returns:
            RateLimitStatus with allowed flag and remaining counts
        """
        if not self.config.enabled:
            return RateLimitStatus(
                allowed=True,
                remaining_minute=999,
                remaining_hour=9999,
                reset_minute_at=time.time() + 60,
                reset_hour_at=time.time() + 3600,
            )

        now = time.time()
        minute_ago = now - 60
        hour_ago = now - 3600

        # Get or initialize request list
        if identifier not in self._requests:
            self._requests[identifier] = []

        # Remove old requests outside the windows
        self._requests[identifier] = [
            ts for ts in self._requests[identifier] if ts > hour_ago
        ]

        # Count requests in each window
        requests_in_minute = sum(1 for ts in self._requests[identifier] if ts > minute_ago)
        requests_in_hour = len(self._requests[identifier])

        # Determine limits based on user vs IP
        if is_user:
            limit_minute = self.config.user_requests_per_minute
            limit_hour = self.config.user_requests_per_hour
        else:
            limit_minute = self.config.ip_requests_per_minute
            limit_hour = self.config.ip_requests_per_hour

        # Check if limits are exceeded
        allowed = requests_in_minute < limit_minute and requests_in_hour < limit_hour

        # Calculate reset times
        if requests_in_minute > 0:
            oldest_minute_request = min(
                (ts for ts in self._requests[identifier] if ts > minute_ago),
                default=now,
            )
            reset_minute_at = oldest_minute_request + 60
        else:
            reset_minute_at = now + 60

        if requests_in_hour > 0:
            oldest_hour_request = min(self._requests[identifier], default=now)
            reset_hour_at = oldest_hour_request + 3600
        else:
            reset_hour_at = now + 3600

        # Record this request if allowed
        if allowed:
            self._requests[identifier].append(now)

        return RateLimitStatus(
            allowed=allowed,
            remaining_minute=max(0, limit_minute - requests_in_minute - (1 if allowed else 0)),
            remaining_hour=max(0, limit_hour - requests_in_hour - (1 if allowed else 0)),
            reset_minute_at=reset_minute_at,
            reset_hour_at=reset_hour_at,
        )

    def reset(self, identifier: Optional[str] = None) -> None:
        """
        Reset rate limit counters.

        Args:
            identifier: Specific identifier to reset, or None to reset all
        """
        if identifier is None:
            self._requests.clear()
        else:
            self._requests.pop(identifier, None)

    def get_stats(self) -> dict[str, int]:
        """Return statistics about tracked identifiers."""
        return {
            "tracked_identifiers": len(self._requests),
            "total_requests": sum(len(reqs) for reqs in self._requests.values()),
        }


# Global rate limiter instance
_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter(config: Optional[RateLimitConfig] = None) -> RateLimiter:
    """Get or create the global rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(config)
    return _rate_limiter


def reset_rate_limiter() -> None:
    """Reset the global rate limiter (useful for testing)."""
    global _rate_limiter
    _rate_limiter = None
