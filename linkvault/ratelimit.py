"""
Rate limiting configuration for LinkVault.

Uses slowapi to provide per-endpoint rate limiting with configurable limits.
Rate limits are applied per IP address for public endpoints and per user
for authenticated endpoints.
"""
from __future__ import annotations

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from linkvault.config import settings


def _get_key_func(request: Request) -> str:
    """
    Key function for rate limiting.
    
    For authenticated requests, uses the API key hash from the Authorization header
    to rate limit per user. For unauthenticated requests, falls back to IP address.
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        # Use the token itself as the key (will be unique per user)
        # We use a prefix to distinguish from IP-based keys
        token = auth_header[7:]  # Strip "Bearer "
        return f"user:{token[:32]}"  # Use first 32 chars to keep key reasonable
    return get_remote_address(request)


def _get_ip_key(request: Request) -> str:
    """Key function that always uses IP address."""
    return get_remote_address(request)


# Create the limiter instance
# Using in-memory storage by default; for production with multiple workers,
# consider using Redis storage
limiter = Limiter(
    key_func=_get_key_func,
    default_limits=[],  # No default limit; each endpoint specifies its own
    enabled=settings.RATE_LIMIT_ENABLED,
)

# Separate limiter for IP-only rate limiting (used for public endpoints)
ip_limiter = Limiter(
    key_func=_get_ip_key,
    default_limits=[],
    enabled=settings.RATE_LIMIT_ENABLED,
)


# Export rate limit strings for use in decorators
RATE_LIMIT_AUTH = settings.RATE_LIMIT_AUTH
RATE_LIMIT_LINKS = settings.RATE_LIMIT_LINKS
RATE_LIMIT_ANALYTICS = settings.RATE_LIMIT_ANALYTICS
RATE_LIMIT_REDIRECTS = settings.RATE_LIMIT_REDIRECTS
