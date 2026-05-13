from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

# SEC-06: bcrypt truncates (or raises in newer versions) passwords longer than
# 72 bytes.  Enforce the limit at the schema level so the API returns a clean
# 422 Unprocessable Content rather than a 500 Internal Server Error.
_PASSWORD_MAX_LENGTH = 72


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=_PASSWORD_MAX_LENGTH)


class UserLogin(BaseModel):
    email: EmailStr
    # SEC-06: also cap login passwords to avoid expensive bcrypt on huge inputs
    password: str = Field(max_length=_PASSWORD_MAX_LENGTH)


class UserResponse(BaseModel):
    id: str
    email: str
    created_at: datetime

    model_config = {"from_attributes": True}


class UserRegisterResponse(BaseModel):
    """Returned on successful registration — includes the plaintext API key (shown once)."""

    id: str
    email: str
    created_at: datetime
    api_key: str  # plaintext, shown only once

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    """Returned by /users/token — the plaintext API key."""

    api_key: str

