"""
Rate limiter implementation for API endpoints.

Supports both per-user and per-IP rate limiting with configurable thresholds.
Uses in-memory storage with automatic expiration of old entries.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional


@dataclass
class RateLimitConfig:
    """Configuration for a rate limit rule."""

    requests_per_minute: int
    requests_per_hour: int


class RateLimiter:
    """
    In-memory rate limiter that tracks requests per identifier (user ID or IP).

    Maintains separate counters for per-minute and per-hour limits.
    Automatically cleans up expired entries to prevent memory bloat.
    """

    def __init__(self, cleanup_interval: int = 3600):
        """
        Initialize the rate limiter.

        Args:
            cleanup_interval: Seconds between cleanup of expired entries (default 1 hour)
        """
        self.cleanup_interval = cleanup_interval
        self.last_cleanup = time.time()

        # Structure: {identifier: [(timestamp, count), ...]}
        # We store tuples of (window_start_time, request_count) for each window
        self.minute_windows: dict[str, list[tuple[float, int]]] = defaultdict(list)
        self.hour_windows: dict[str, list[tuple[float, int]]] = defaultdict(list)

    def _cleanup_expired(self) -> None:
        """Remove expired entries from tracking dictionaries."""
        now = time.time()
        if now - self.last_cleanup < self.cleanup_interval:
            return

        # Clean up minute windows (older than 60 seconds)
        for identifier in list(self.minute_windows.keys()):
            self.minute_windows[identifier] = [
                (ts, count)
                for ts, count in self.minute_windows[identifier]
                if now - ts < 60
            ]
            if not self.minute_windows[identifier]:
                del self.minute_windows[identifier]

        # Clean up hour windows (older than 3600 seconds)
        for identifier in list(self.hour_windows.keys()):
            self.hour_windows[identifier] = [
                (ts, count)
                for ts, count in self.hour_windows[identifier]
                if now - ts < 3600
            ]
            if not self.hour_windows[identifier]:
                del self.hour_windows[identifier]

        self.last_cleanup = now

    def is_allowed(
        self,
        identifier: str,
        config: RateLimitConfig,
    ) -> tuple[bool, dict[str, int]]:
        """
        Check if a request is allowed under the rate limit.

        Args:
            identifier: Unique identifier (user ID or IP address)
            config: Rate limit configuration

        Returns:
            Tuple of (is_allowed, limit_info) where limit_info contains:
            - limit_per_minute: configured limit
            - remaining_per_minute: requests remaining this minute
            - reset_minute_at: unix timestamp when minute counter resets
            - limit_per_hour: configured limit
            - remaining_per_hour: requests remaining this hour
            - reset_hour_at: unix timestamp when hour counter resets
        """
        self._cleanup_expired()
        now = time.time()

        # Calculate minute window (current minute)
        minute_window_start = int(now / 60) * 60
        minute_reset_at = minute_window_start + 60

        # Calculate hour window (current hour)
        hour_window_start = int(now / 3600) * 3600
        hour_reset_at = hour_window_start + 3600

        # Count requests in current minute window
        minute_count = 0
        for ts, count in self.minute_windows[identifier]:
            if ts == minute_window_start:
                minute_count = count
                break

        # Count requests in current hour window
        hour_count = 0
        for ts, count in self.hour_windows[identifier]:
            if ts == hour_window_start:
                hour_count = count
                break

        # Check limits
        minute_exceeded = minute_count >= config.requests_per_minute
        hour_exceeded = hour_count >= config.requests_per_hour

        limit_info = {
            "limit_per_minute": config.requests_per_minute,
            "remaining_per_minute": max(0, config.requests_per_minute - minute_count),
            "reset_minute_at": minute_reset_at,
            "limit_per_hour": config.requests_per_hour,
            "remaining_per_hour": max(0, config.requests_per_hour - hour_count),
            "reset_hour_at": hour_reset_at,
        }

        if minute_exceeded or hour_exceeded:
            return False, limit_info

        # Increment counters
        # Update or create minute window entry
        minute_found = False
        for i, (ts, count) in enumerate(self.minute_windows[identifier]):
            if ts == minute_window_start:
                self.minute_windows[identifier][i] = (ts, count + 1)
                minute_found = True
                break
        if not minute_found:
            self.minute_windows[identifier].append((minute_window_start, 1))

        # Update or create hour window entry
        hour_found = False
        for i, (ts, count) in enumerate(self.hour_windows[identifier]):
            if ts == hour_window_start:
                self.hour_windows[identifier][i] = (ts, count + 1)
                hour_found = True
                break
        if not hour_found:
            self.hour_windows[identifier].append((hour_window_start, 1))

        return True, limit_info

    def get_limit_info(
        self,
        identifier: str,
        config: RateLimitConfig,
    ) -> dict[str, int]:
        """
        Get current rate limit info without incrementing counters.

        Useful for informational endpoints or debugging.
        """
        self._cleanup_expired()
        now = time.time()

        minute_window_start = int(now / 60) * 60
        minute_reset_at = minute_window_start + 60

        hour_window_start = int(now / 3600) * 3600
        hour_reset_at = hour_window_start + 3600

        minute_count = 0
        for ts, count in self.minute_windows[identifier]:
            if ts == minute_window_start:
                minute_count = count
                break

        hour_count = 0
        for ts, count in self.hour_windows[identifier]:
            if ts == hour_window_start:
                hour_count = count
                break

        return {
            "limit_per_minute": config.requests_per_minute,
            "remaining_per_minute": max(0, config.requests_per_minute - minute_count),
            "reset_minute_at": minute_reset_at,
            "limit_per_hour": config.requests_per_hour,
            "remaining_per_hour": max(0, config.requests_per_hour - hour_count),
            "reset_hour_at": hour_reset_at,
        }


# Global rate limiter instance
_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """Get or create the global rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter

