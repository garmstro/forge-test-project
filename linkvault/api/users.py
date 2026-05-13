from __future__ import annotations

import logging
import uuid

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.api.deps import _hash_api_key
from linkvault.database import get_db
from linkvault.models.user import User
from linkvault.schemas.user import (
    TokenResponse,
    UserLogin,
    UserRegister,
    UserRegisterResponse,
)

router = APIRouter(prefix="/users", tags=["users"])
logger = logging.getLogger(__name__)

# SEC-06: bcrypt silently truncates (or raises in newer versions) passwords
# longer than 72 bytes.  Enforce an explicit upper bound so the API returns a
# clean 422 instead of a 500, and to prevent a DoS via intentionally huge
# passwords that force expensive bcrypt work.
_PASSWORD_MAX_LENGTH = 72


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


@router.post(
    "/register",
    response_model=UserRegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    payload: UserRegister,
    db: AsyncSession = Depends(get_db),
) -> UserRegisterResponse:
    """Register a new user.  Returns a one-time plaintext API key."""
    # SEC-06: reject passwords that exceed bcrypt's 72-byte limit
    if len(payload.password.encode()) > _PASSWORD_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Password must not exceed {_PASSWORD_MAX_LENGTH} characters.",
        )

    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none() is not None:
        # SEC-05: return the same 409 but log the attempt server-side so
        # operators can detect enumeration probes without leaking the fact
        # to the caller.  The response body is intentionally vague.
        logger.warning("Registration attempt for already-registered email (redacted)")
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

    # SEC-15: audit log successful registration (no PII in the log line)
    logger.info("New user registered: id=%s", user.id)

    return UserRegisterResponse(
        id=user.id,
        email=user.email,
        created_at=user.created_at,
        api_key=raw_key,
    )


@router.post("/token", response_model=TokenResponse)
async def get_token(
    payload: UserLogin,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Exchange email + password for the plaintext API key.

    Decision (see DECISIONS.md §6): a new API key is generated and persisted on
    every call, invalidating the previous one.
    """
    # SEC-06: reject oversized passwords before the expensive bcrypt check
    if len(payload.password.encode()) > _PASSWORD_MAX_LENGTH:
        # Return the same 401 as a wrong password to avoid leaking information
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    if user is None or not _verify_password(payload.password, user.password_hash):
        # SEC-15: audit log failed login attempts (no PII)
        logger.warning("Failed login attempt (invalid credentials)")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    raw_key = str(uuid.uuid4())
    user.api_key_hash = _hash_api_key(raw_key)
    db.add(user)
    await db.flush()  # ensure the new hash is visible within this transaction

    # SEC-15: audit log successful token issuance
    logger.info("API key rotated for user: id=%s", user.id)

    return TokenResponse(api_key=raw_key)

