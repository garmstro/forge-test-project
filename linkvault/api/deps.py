from __future__ import annotations

import hashlib

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.config import settings
from linkvault.database import get_db
from linkvault.models.user import User
from linkvault.services.ratelimit import RateLimitConfig, get_limiter

bearer_scheme = HTTPBearer()


def _hash_api_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest of the raw API key."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


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


class RateLimitExceeded(HTTPException):
    """Exception raised when a rate limit is exceeded."""

    def __init__(self, retry_after: int | None = None) -> None:
        """Initialize the exception.

        Args:
            retry_after: Number of seconds to wait before retrying.
        """
        headers = {}
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)

        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please try again later.",
            headers=headers,
        )


async def check_rate_limit_by_ip(
    request: Request,
    max_requests: int,
    window_seconds: int,
) -> None:
    """Check rate limit by client IP address.

    Args:
        request: FastAPI request object.
        max_requests: Maximum requests allowed in the window.
        window_seconds: Time window in seconds.

    Raises:
        RateLimitExceeded: If rate limit is exceeded.
    """
    if not settings.RATE_LIMIT_ENABLED:
        return

    client_ip = request.client.host if request.client else "unknown"
    limiter = get_limiter()
    config = RateLimitConfig(max_requests=max_requests, window_seconds=window_seconds)

    if not limiter.is_allowed(client_ip, config):
        retry_after = int(limiter.get_reset_time(client_ip, config) - __import__("time").time())
        raise RateLimitExceeded(retry_after=max(1, retry_after))


async def check_rate_limit_by_user(
    user: User,
    max_requests: int,
    window_seconds: int,
) -> None:
    """Check rate limit by authenticated user ID.

    Args:
        user: Authenticated user object.
        max_requests: Maximum requests allowed in the window.
        window_seconds: Time window in seconds.

    Raises:
        RateLimitExceeded: If rate limit is exceeded.
    """
    if not settings.RATE_LIMIT_ENABLED:
        return

    limiter = get_limiter()
    config = RateLimitConfig(max_requests=max_requests, window_seconds=window_seconds)

    if not limiter.is_allowed(str(user.id), config):
        retry_after = int(limiter.get_reset_time(str(user.id), config) - __import__("time").time())
        raise RateLimitExceeded(retry_after=max(1, retry_after))

