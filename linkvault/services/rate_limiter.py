"""Rate limiting service for API requests.

Supports both user-based (via API key) and IP-based rate limiting.
Uses an in-memory store with TTL-based expiration.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""

    # User-based limits (authenticated requests)
    user_requests_per_minute: int = 60
    user_requests_per_hour: int = 1000

    # IP-based limits (unauthenticated requests)
    ip_requests_per_minute: int = 30
    ip_requests_per_hour: int = 500


@dataclass
class RequestWindow:
    """Tracks requests within a time window."""

    count: int = 0
    window_start: float = field(default_factory=time.time)

    def is_expired(self, window_seconds: int) -> bool:
        """Check if this window has expired."""
        return time.time() - self.window_start > window_seconds

    def reset_if_expired(self, window_seconds: int) -> None:
        """Reset the window if it has expired."""
        if self.is_expired(window_seconds):
            self.count = 0
            self.window_start = time.time()

    def increment(self) -> None:
        """Increment the request count."""
        self.count += 1


class InMemoryRateLimiter:
    """In-memory rate limiter using sliding windows."""

    def __init__(self, config: Optional[RateLimitConfig] = None):
        """Initialize the rate limiter.

        Args:
            config: Rate limit configuration. Uses defaults if not provided.
        """
        self.config = config or RateLimitConfig()

        # Store request windows: {key: {window_type: RequestWindow}}
        self._user_minute_windows: dict[str, RequestWindow] = defaultdict(RequestWindow)
        self._user_hour_windows: dict[str, RequestWindow] = defaultdict(RequestWindow)
        self._ip_minute_windows: dict[str, RequestWindow] = defaultdict(RequestWindow)
        self._ip_hour_windows: dict[str, RequestWindow] = defaultdict(RequestWindow)

    def check_user_limit(self, user_id: str) -> tuple[bool, dict[str, int]]:
        """Check if a user has exceeded rate limits.

        Args:
            user_id: The user identifier (typically user.id or user.email).

        Returns:
            Tuple of (is_allowed, stats) where stats contains:
            - user_minute_remaining: Requests remaining in current minute
            - user_hour_remaining: Requests remaining in current hour
        """
        # Check minute limit
        minute_window = self._user_minute_windows[user_id]
        minute_window.reset_if_expired(60)

        if minute_window.count >= self.config.user_requests_per_minute:
            return False, {
                "user_minute_remaining": 0,
                "user_hour_remaining": max(
                    0,
                    self.config.user_requests_per_hour
                    - self._user_hour_windows[user_id].count,
                ),
            }

        # Check hour limit
        hour_window = self._user_hour_windows[user_id]
        hour_window.reset_if_expired(3600)

        if hour_window.count >= self.config.user_requests_per_hour:
            return False, {
                "user_minute_remaining": max(
                    0,
                    self.config.user_requests_per_minute - minute_window.count,
                ),
                "user_hour_remaining": 0,
            }

        # Both limits OK
        minute_window.increment()
        hour_window.increment()

        return True, {
            "user_minute_remaining": max(
                0, self.config.user_requests_per_minute - minute_window.count
            ),
            "user_hour_remaining": max(
                0, self.config.user_requests_per_hour - hour_window.count
            ),
        }

    def check_ip_limit(self, ip_address: str) -> tuple[bool, dict[str, int]]:
        """Check if an IP address has exceeded rate limits.

        Args:
            ip_address: The IP address.

        Returns:
            Tuple of (is_allowed, stats) where stats contains:
            - ip_minute_remaining: Requests remaining in current minute
            - ip_hour_remaining: Requests remaining in current hour
        """
        # Check minute limit
        minute_window = self._ip_minute_windows[ip_address]
        minute_window.reset_if_expired(60)

        if minute_window.count >= self.config.ip_requests_per_minute:
            return False, {
                "ip_minute_remaining": 0,
                "ip_hour_remaining": max(
                    0,
                    self.config.ip_requests_per_hour
                    - self._ip_hour_windows[ip_address].count,
                ),
            }

        # Check hour limit
        hour_window = self._ip_hour_windows[ip_address]
        hour_window.reset_if_expired(3600)

        if hour_window.count >= self.config.ip_requests_per_hour:
            return False, {
                "ip_minute_remaining": max(
                    0, self.config.ip_requests_per_minute - minute_window.count
                ),
                "ip_hour_remaining": 0,
            }

        # Both limits OK
        minute_window.increment()
        hour_window.increment()

        return True, {
            "ip_minute_remaining": max(
                0, self.config.ip_requests_per_minute - minute_window.count
            ),
            "ip_hour_remaining": max(
                0, self.config.ip_requests_per_hour - hour_window.count
            ),
        }

    def reset_user(self, user_id: str) -> None:
        """Reset rate limit counters for a user (useful for testing)."""
        self._user_minute_windows.pop(user_id, None)
        self._user_hour_windows.pop(user_id, None)

    def reset_ip(self, ip_address: str) -> None:
        """Reset rate limit counters for an IP address (useful for testing)."""
        self._ip_minute_windows.pop(ip_address, None)
        self._ip_hour_windows.pop(ip_address, None)

    def reset_all(self) -> None:
        """Reset all rate limit counters (useful for testing)."""
        self._user_minute_windows.clear()
        self._user_hour_windows.clear()
        self._ip_minute_windows.clear()
        self._ip_hour_windows.clear()
