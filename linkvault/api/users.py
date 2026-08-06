from __future__ import annotations

import hashlib
import uuid

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.api.deps import check_rate_limit_by_ip
from linkvault.config import settings
from linkvault.database import get_db
from linkvault.models.user import User
from linkvault.schemas.user import (
    TokenResponse,
    UserLogin,
    UserRegister,
    UserRegisterResponse,
)

router = APIRouter(prefix="/users", tags=["users"])


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def _hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


@router.post(
    "/register",
    response_model=UserRegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    payload: UserRegister,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UserRegisterResponse:
    """Register a new user.  Returns a one-time plaintext API key."""
    # Rate limit: 5 requests per hour per IP
    await check_rate_limit_by_ip(
        request,
        max_requests=settings.RATE_LIMIT_REGISTER_PER_HOUR,
        window_seconds=3600,
    )

    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with that email already exists.",
        )

    raw_key = str(uuid.uuid4())

    user = User(
        email=payload.email,
        password_hash=_hash_password(payload.password),
        api_key_hash=_hash_api_key(raw_key),
    )
    db.add(user)
    await db.flush()  # populate defaults (id, created_at) before we read them

    return UserRegisterResponse(
        id=user.id,
        email=user.email,
        created_at=user.created_at,
        api_key=raw_key,
    )


@router.post("/token", response_model=TokenResponse)
async def get_token(
    payload: UserLogin,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Exchange email + password for the plaintext API key.

    Decision (see DECISIONS.md §6): a new API key is generated and persisted on
    every call, invalidating the previous one.
    """
    # Rate limit: 10 requests per minute per IP
    await check_rate_limit_by_ip(
        request,
        max_requests=settings.RATE_LIMIT_TOKEN_PER_MINUTE,
        window_seconds=60,
    )

    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    if user is None or not _verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    raw_key = str(uuid.uuid4())
    user.api_key_hash = _hash_api_key(raw_key)
    db.add(user)
    await db.flush()  # ensure the new hash is visible within this transaction

    return TokenResponse(api_key=raw_key)

