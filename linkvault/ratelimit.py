"""
Rate limiting configuration for LinkVault using slowapi.

This module provides a shared Limiter instance and key functions for
extracting rate limit identifiers from requests.
"""
from __future__ import annotations

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from linkvault.config import settings


def get_api_key_or_ip(request: Request) -> str:
    """Extract the Bearer token from Authorization header, falling back to IP.

    This allows authenticated endpoints to rate limit per API key rather than
    per IP, so each user gets their own bucket.
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        # Use the token as the rate limit key
        return auth_header[7:]
    # Fall back to IP address
    return get_remote_address(request)


def _get_enabled_state() -> bool:
    """Return whether rate limiting is enabled (for dynamic checking)."""
    return settings.RATE_LIMIT_ENABLED


# Create the shared limiter instance with in-memory storage
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.RATE_LIMIT_DEFAULT],
    enabled=settings.RATE_LIMIT_ENABLED,
)
