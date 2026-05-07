from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


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

