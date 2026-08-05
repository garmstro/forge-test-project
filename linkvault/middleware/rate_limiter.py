"""Rate limiting middleware using slowapi.

This module provides rate limiting for LinkVault endpoints:
- Public endpoints (registration, login, redirects) are limited by IP address
- Authenticated endpoints are limited by user ID
"""
from __future__ import annotations

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def get_rate_limit_key(request: Request) -> str:
    """Extract the rate limit key from the request.
    
    For authenticated requests (with user in request.state), use the user ID.
    Otherwise, fall back to IP address.
    """
    # Check if user is attached to request state (set by auth dependency)
    if hasattr(request.state, "user") and request.state.user is not None:
        return f"user:{request.state.user.id}"
    
    # Fall back to IP address for unauthenticated requests
    return f"ip:{get_remote_address(request)}"


# Create the limiter instance with default key function
limiter = Limiter(key_func=get_rate_limit_key)
