"""
Rate limiting configuration for the LinkVault API.

Uses slowapi to provide per-IP rate limiting with configurable limits.
"""
from __future__ import annotations

from fastapi import Request, Response
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse

from linkvault.config import settings


def get_client_ip(request: Request) -> str:
    """
    Extract client IP address from request.
    
    Checks X-Forwarded-For header first (for reverse proxy setups),
    then falls back to the direct remote address.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # X-Forwarded-For can contain multiple IPs; the first is the client
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)


# Create the limiter instance with IP-based key function
limiter = Limiter(
    key_func=get_client_ip,
    enabled=settings.RATE_LIMIT_ENABLED,
    default_limits=[settings.RATE_LIMIT_DEFAULT],
)


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> Response:
    """
    Custom handler for rate limit exceeded errors.
    
    Returns a JSON response with consistent error envelope format.
    """
    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "detail": f"Rate limit exceeded: {exc.detail}",
        },
        headers={"Retry-After": str(exc.retry_after) if exc.retry_after else "60"},
    )
