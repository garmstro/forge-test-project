from __future__ import annotations

import hashlib

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.database import get_db
from linkvault.models.user import User
from linkvault.services.rate_limit import RateLimitService

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


async def check_user_rate_limit(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """
    FastAPI dependency that checks rate limits for authenticated users.
    
    Returns rate limit info dict for use in response headers.
    Raises 429 if the user has exceeded their rate limit.
    """
    is_allowed, info = await RateLimitService.check_user_rate_limit(
        current_user.id, db
    )
    
    if not is_allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please try again later.",
            headers={
                "X-RateLimit-Limit": str(info["limit"]),
                "X-RateLimit-Remaining": str(info["remaining"]),
                "X-RateLimit-Reset": str(info["reset_at"]),
            },
        )
    
    return info


async def check_ip_rate_limit(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """
    FastAPI dependency that checks rate limits for IP addresses (public endpoints).
    
    Returns rate limit info dict for use in response headers.
    Raises 429 if the IP has exceeded their rate limit.
    """
    ip_address = request.client.host if request.client else "unknown"
    
    is_allowed, info = await RateLimitService.check_ip_rate_limit(ip_address, db)
    
    if not is_allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please try again later.",
            headers={
                "X-RateLimit-Limit": str(info["limit"]),
                "X-RateLimit-Remaining": str(info["remaining"]),
                "X-RateLimit-Reset": str(info["reset_at"]),
            },
        )
    
    return info
