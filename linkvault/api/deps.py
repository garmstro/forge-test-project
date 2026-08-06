from __future__ import annotations

import hashlib
import logging

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.config import settings
from linkvault.database import get_db
from linkvault.models.user import User
from linkvault.services.rate_limiter import (
    RateLimitConfig,
    RateLimitStatus,
    get_rate_limiter,
)

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer()


def _hash_api_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest of the raw API key."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _get_client_ip(request: Request) -> str:
    """Extract client IP from request, handling proxies."""
    # Check for X-Forwarded-For header (common with reverse proxies)
    if "x-forwarded-for" in request.headers:
        return request.headers["x-forwarded-for"].split(",")[0].strip()
    # Fall back to direct connection IP
    return request.client.host if request.client else "unknown"


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    FastAPI dependency that validates the Bearer API key and returns the
    corresponding User.  Raises 401 on any failure.
    """
    raw_key = credentials.credentials
    key_hash = _hash_api_key(raw_key)

    result = await db.execute(select(User).where(User.api_key_hash == key_hash))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def check_rate_limit_user(
    current_user: User = Depends(get_current_user),
) -> RateLimitStatus:
    """
    FastAPI dependency that checks rate limits for authenticated users.
    Raises 429 (Too Many Requests) if the user has exceeded their limit.
    """
    # Initialize rate limiter with config from settings
    config = RateLimitConfig(
        user_requests_per_minute=settings.RATE_LIMIT_USER_PER_MINUTE,
        user_requests_per_hour=settings.RATE_LIMIT_USER_PER_HOUR,
        ip_requests_per_minute=settings.RATE_LIMIT_IP_PER_MINUTE,
        ip_requests_per_hour=settings.RATE_LIMIT_IP_PER_HOUR,
        enabled=settings.RATE_LIMITING_ENABLED,
    )
    limiter = get_rate_limiter(config)

    status = limiter.check_limit(current_user.id, is_user=True)

    if not status.allowed:
        logger.warning(
            "Rate limit exceeded for user %s (minute: %d, hour: %d)",
            current_user.id,
            settings.RATE_LIMIT_USER_PER_MINUTE,
            settings.RATE_LIMIT_USER_PER_HOUR,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please try again later.",
            headers={
                "Retry-After": str(int(status.reset_minute_at)),
                "X-RateLimit-Limit-Minute": str(settings.RATE_LIMIT_USER_PER_MINUTE),
                "X-RateLimit-Limit-Hour": str(settings.RATE_LIMIT_USER_PER_HOUR),
                "X-RateLimit-Remaining-Minute": str(status.remaining_minute),
                "X-RateLimit-Remaining-Hour": str(status.remaining_hour),
            },
        )

    return status


async def check_rate_limit_ip(request: Request) -> RateLimitStatus:
    """
    FastAPI dependency that checks rate limits for unauthenticated requests by IP.
    Raises 429 (Too Many Requests) if the IP has exceeded their limit.
    """
    # Initialize rate limiter with config from settings
    config = RateLimitConfig(
        user_requests_per_minute=settings.RATE_LIMIT_USER_PER_MINUTE,
        user_requests_per_hour=settings.RATE_LIMIT_USER_PER_HOUR,
        ip_requests_per_minute=settings.RATE_LIMIT_IP_PER_MINUTE,
        ip_requests_per_hour=settings.RATE_LIMIT_IP_PER_HOUR,
        enabled=settings.RATE_LIMITING_ENABLED,
    )
    limiter = get_rate_limiter(config)

    client_ip = _get_client_ip(request)
    status = limiter.check_limit(client_ip, is_user=False)

    if not status.allowed:
        logger.warning(
            "Rate limit exceeded for IP %s (minute: %d, hour: %d)",
            client_ip,
            settings.RATE_LIMIT_IP_PER_MINUTE,
            settings.RATE_LIMIT_IP_PER_HOUR,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please try again later.",
            headers={
                "Retry-After": str(int(status.reset_minute_at)),
                "X-RateLimit-Limit-Minute": str(settings.RATE_LIMIT_IP_PER_MINUTE),
                "X-RateLimit-Limit-Hour": str(settings.RATE_LIMIT_IP_PER_HOUR),
                "X-RateLimit-Remaining-Minute": str(status.remaining_minute),
                "X-RateLimit-Remaining-Hour": str(status.remaining_hour),
            },
        )

    return status
