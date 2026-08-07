"""
Rate limiting configuration for LinkVault API.

Uses slowapi with in-memory storage. For multi-worker deployments,
consider switching to Redis storage for shared state.
"""
from __future__ import annotations

from typing import Callable

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from linkvault.config import settings


def _get_api_key_or_ip(request: Request) -> str:
    """
    Key function for authenticated endpoints.
    
    Returns the API key from the Authorization header if present,
    otherwise falls back to the client IP address.
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        # Use the API key (token) as the rate limit key
        return auth_header[7:]  # Strip "Bearer " prefix
    # Fall back to IP address for unauthenticated requests
    return get_remote_address(request)


def _get_ip_address(request: Request) -> str:
    """Key function for public endpoints — uses client IP address."""
    return get_remote_address(request)


# Create the limiter instance with in-memory storage
# The key_func is set per-endpoint, but we need a default
limiter = Limiter(
    key_func=_get_ip_address,
    enabled=settings.RATE_LIMIT_ENABLED,
    default_limits=[],  # No default limits; we apply them explicitly
)


def get_auth_limiter() -> Callable[[Request], str]:
    """Return the key function for auth endpoints (IP-based)."""
    return _get_ip_address


def get_user_limiter() -> Callable[[Request], str]:
    """Return the key function for authenticated endpoints (API key-based)."""
    return _get_api_key_or_ip


def get_redirect_limiter() -> Callable[[Request], str]:
    """Return the key function for redirect endpoints (IP-based)."""
    return _get_ip_address
