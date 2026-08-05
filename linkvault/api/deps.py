from __future__ import annotations

import hashlib

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.database import get_db
from linkvault.models.user import User

bearer_scheme = HTTPBearer()


def _hash_api_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest of the raw API key."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    FastAPI dependency that validates the Bearer API key and returns the
    corresponding User.  Raises 401 on any failure.
    
    Also attaches the user to request.state for rate limiting purposes.
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
    
    # Attach user to request state for rate limiting
    request.state.user = user
    
    return user
