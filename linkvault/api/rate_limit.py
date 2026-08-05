"""Rate limiting utilities for LinkVault API.

This module provides rate limiting middleware and decorators for protecting
the API against abuse. Different endpoints have different rate limits:

- Redirect endpoint (/{slug}): 600 requests/minute per IP (10 req/sec)
- Auth endpoints (register, token): 10 requests/minute per IP
- Link management endpoints: 60 requests/minute per authenticated user
- Analytics endpoints: 30 requests/minute per authenticated user
"""
from __future__ import annotations

import logging
from typing import Callable

from fastapi import Request, status
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from linkvault.config import settings

logger = logging.getLogger(__name__)

# Initialize the limiter with a memory store
limiter = Limiter(key_func=get_remote_address)


def get_rate_limit_key_by_user(request: Request) -> str:
    """Generate a rate limit key based on authenticated user ID.
    
    Falls back to IP address if user is not authenticated.
    """
    # Try to get user_id from request state (set by auth middleware)
    if hasattr(request.state, "user_id"):
        return f"user:{request.state.user_id}"
    # Fall back to IP address
    return get_remote_address(request)


async def rate_limit_exception_handler(
    request: Request, exc: RateLimitExceeded
) -> JSONResponse:
    """Custom exception handler for rate limit exceeded errors."""
    logger.warning(
        "Rate limit exceeded for %s: %s",
        get_remote_address(request),
        exc.detail,
    )
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "error": "rate_limit_exceeded",
            "detail": "Too many requests. Please try again later.",
        },
    )


def is_rate_limiting_enabled() -> bool:
    """Check if rate limiting is enabled in configuration."""
    return settings.RATE_LIMIT_ENABLED
