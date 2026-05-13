from __future__ import annotations

import hashlib
import hmac

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.config import settings
from linkvault.database import get_db
from linkvault.models.user import User

bearer_scheme = HTTPBearer()


def _hash_api_key(raw_key: str) -> str:
    """Return an HMAC-SHA256 hex digest of the raw API key, keyed with SECRET_KEY.

    SEC-02: A bare SHA-256 hash is a fast, unkeyed hash — if the database is
    compromised an attacker can brute-force API keys offline at billions of
    guesses per second.  Keying the hash with SECRET_KEY (a server-side secret
    that is *not* stored in the database) means the attacker also needs the
    secret key to mount an offline attack, dramatically raising the cost.
    """
    return hmac.new(
        settings.SECRET_KEY.encode(),
        raw_key.encode(),
        hashlib.sha256,
    ).hexdigest()


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

