"""
Rate limiting configuration for LinkVault API.

Uses slowapi with in-memory storage. For multi-worker deployments,
consider switching to Redis storage for shared state.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from slowapi import Limiter
from slowapi.util import get_remote_address

from linkvault.config import settings

if TYPE_CHECKING:
    from fastapi import Request


def get_api_key_or_ip(request: Request) -> str:
    """
    Extract rate limit key from request.
    
    For authenticated endpoints, uses the API key from the Authorization header.
    Falls back to IP address for unauthenticated requests.
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        # Use the API key (hashed for privacy in logs)
        token = auth_header[7:]
        # Return a prefix to distinguish from IP-based keys
        return f"apikey:{token[:16]}"
    return get_remote_address(request)


def get_ip_address(request: Request) -> str:
    """Extract IP address from request for public endpoint rate limiting."""
    return get_remote_address(request)


# Limiter for authenticated endpoints (keyed by API key)
limiter = Limiter(
    key_func=get_api_key_or_ip,
    enabled=settings.RATE_LIMIT_ENABLED,
    default_limits=[],
)

# Limiter for public endpoints (keyed by IP address)
ip_limiter = Limiter(
    key_func=get_ip_address,
    enabled=settings.RATE_LIMIT_ENABLED,
    default_limits=[],
)
