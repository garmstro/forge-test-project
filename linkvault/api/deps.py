from __future__ import annotations

import hashlib

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.config import settings
from linkvault.database import get_db
from linkvault.models.user import User
from linkvault.services.ratelimit import (
    RateLimitExceeded,
    SlidingWindowRateLimiter,
    api_limiter,
    redirect_limiter,
)

bearer_scheme = HTTPBearer()


def _client_key(request: Request) -> str:
    """Rate-limit key: the direct peer address.

    X-Forwarded-For is deliberately ignored — it is client-controlled and
    would let anyone pick their own bucket.
    """
    return request.client.host if request.client else "unknown"


def _enforce(request: Request, limiter: SlidingWindowRateLimiter, limit: int) -> None:
    if not settings.RATE_LIMIT_ENABLED:
        return
    decision = limiter.hit(_client_key(request), limit)
    if not decision.allowed:
        raise RateLimitExceeded(retry_after=decision.retry_after)


async def rate_limit_redirect(request: Request) -> None:
    """Per-IP limit for GET /{slug}; runs before the DB lookup."""
    _enforce(request, redirect_limiter, settings.RATE_LIMIT_REDIRECT_PER_MINUTE)


async def rate_limit_api(request: Request) -> None:
    """Per-IP limit shared by POST /users/register and POST /users/token."""
    _enforce(request, api_limiter, settings.RATE_LIMIT_API_PER_MINUTE)


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

