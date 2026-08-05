"""
Rate limiting service for LinkVault.

Implements per-user and per-IP rate limiting with configurable windows and limits.
Uses an in-memory store with automatic cleanup of expired entries.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""

    # Per-user limits (authenticated requests)
    user_requests_per_minute: int = 60
    user_requests_per_hour: int = 1000

    # Per-IP limits (unauthenticated requests)
    ip_requests_per_minute: int = 30
    ip_requests_per_hour: int = 500

    # Cleanup interval (seconds) — remove expired entries
    cleanup_interval: int = 300


class RateLimiter:
    """
    In-memory rate limiter with per-user and per-IP tracking.

    Tracks request counts in sliding windows (1 minute and 1 hour).
    Automatically cleans up expired entries every cleanup_interval seconds.
    """

    def __init__(self, config: Optional[RateLimitConfig] = None):
        self.config = config or RateLimitConfig()

        # Store: {key: [(timestamp, count), ...]}
        # We use a list of (timestamp, count) tuples to track requests in a window
        self._user_requests: dict[str, list[float]] = defaultdict(list)
        self._ip_requests: dict[str, list[float]] = defaultdict(list)

        # Cleanup task
        self._cleanup_task: Optional[asyncio.Task[None]] = None
        self._running = False

    async def start(self) -> None:
        """Start the background cleanup task."""
        if self._running:
            return
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        """Stop the background cleanup task."""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

    async def _cleanup_loop(self) -> None:
        """Periodically clean up expired entries."""
        while self._running:
            try:
                await asyncio.sleep(self.config.cleanup_interval)
                self._cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception:
                # Log but don't crash
                pass

    def _cleanup_expired(self) -> None:
        """Remove entries older than 1 hour."""
        now = time.time()
        cutoff = now - 3600  # 1 hour

        for key in list(self._user_requests.keys()):
            self._user_requests[key] = [ts for ts in self._user_requests[key] if ts > cutoff]
            if not self._user_requests[key]:
                del self._user_requests[key]

        for key in list(self._ip_requests.keys()):
            self._ip_requests[key] = [ts for ts in self._ip_requests[key] if ts > cutoff]
            if not self._ip_requests[key]:
                del self._ip_requests[key]

    def _count_requests_in_window(
        self, timestamps: list[float], window_seconds: int
    ) -> int:
        """Count requests within the last window_seconds."""
        now = time.time()
        cutoff = now - window_seconds
        return sum(1 for ts in timestamps if ts > cutoff)

    async def check_user_limit(self, user_id: str) -> tuple[bool, dict[str, int]]:
        """
        Check if a user has exceeded rate limits.

        Returns:
            (allowed, info) where:
            - allowed: True if request is allowed
            - info: dict with current counts and limits
        """
        now = time.time()
        self._user_requests[user_id].append(now)

        # Count requests in each window
        count_1m = self._count_requests_in_window(self._user_requests[user_id], 60)
        count_1h = self._count_requests_in_window(self._user_requests[user_id], 3600)

        allowed = (
            count_1m <= self.config.user_requests_per_minute
            and count_1h <= self.config.user_requests_per_hour
        )

        return allowed, {
            "requests_1m": count_1m,
            "limit_1m": self.config.user_requests_per_minute,
            "requests_1h": count_1h,
            "limit_1h": self.config.user_requests_per_hour,
        }

    async def check_ip_limit(self, ip: str) -> tuple[bool, dict[str, int]]:
        """
        Check if an IP has exceeded rate limits.

        Returns:
            (allowed, info) where:
            - allowed: True if request is allowed
            - info: dict with current counts and limits
        """
        now = time.time()
        self._ip_requests[ip].append(now)

        # Count requests in each window
        count_1m = self._count_requests_in_window(self._ip_requests[ip], 60)
        count_1h = self._count_requests_in_window(self._ip_requests[ip], 3600)

        allowed = (
            count_1m <= self.config.ip_requests_per_minute
            and count_1h <= self.config.ip_requests_per_hour
        )

        return allowed, {
            "requests_1m": count_1m,
            "limit_1m": self.config.ip_requests_per_minute,
            "requests_1h": count_1h,
            "limit_1h": self.config.ip_requests_per_hour,
        }

    def reset_user(self, user_id: str) -> None:
        """Reset rate limit for a specific user (useful for testing)."""
        if user_id in self._user_requests:
            del self._user_requests[user_id]

    def reset_ip(self, ip: str) -> None:
        """Reset rate limit for a specific IP (useful for testing)."""
        if ip in self._ip_requests:
            del self._ip_requests[ip]

    def reset_all(self) -> None:
        """Reset all rate limits (useful for testing)."""
        self._user_requests.clear()
        self._ip_requests.clear()
