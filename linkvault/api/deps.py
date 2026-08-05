from __future__ import annotations

import hashlib

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.database import get_db
from linkvault.models.user import User
from linkvault.services.rate_limiter import RateLimiter

bearer_scheme = HTTPBearer()

# Global rate limiter instance
_rate_limiter: RateLimiter | None = None


def set_rate_limiter(limiter: RateLimiter) -> None:
    """Set the global rate limiter instance."""
    global _rate_limiter
    _rate_limiter = limiter


def get_rate_limiter() -> RateLimiter:
    """Get the global rate limiter instance."""
    if _rate_limiter is None:
        raise RuntimeError("Rate limiter not initialized")
    return _rate_limiter


def _hash_api_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest of the raw API key."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
) -> User:
    """
    FastAPI dependency that validates the Bearer API key and returns the
    corresponding User.  Raises 401 on any failure.
    
    Also checks per-user rate limits if rate limiting is enabled.
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

    # Check per-user rate limit
    if _rate_limiter is not None:
        allowed, info = await _rate_limiter.check_user_limit(user.id)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. {info['requests_1m']}/{info['limit_1m']} requests per minute.",
                headers={
                    "Retry-After": "60",
                    "X-RateLimit-Limit-Minute": str(info["limit_1m"]),
                    "X-RateLimit-Remaining-Minute": str(
                        max(0, info["limit_1m"] - info["requests_1m"])
                    ),
                },
            )

    return user

