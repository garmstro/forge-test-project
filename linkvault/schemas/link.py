from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, HttpUrl, field_validator


class LinkCreate(BaseModel):
    url: str
    slug: str | None = None
    expires_at: datetime | None = None
    max_clicks: int | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class LinkUpdate(BaseModel):
    url: str | None = None
    expires_at: datetime | None = None
    max_clicks: int | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        if v is not None and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class LinkResponse(BaseModel):
    id: str
    user_id: str
    slug: str
    destination_url: str
    expires_at: datetime | None
    max_clicks: int | None
    click_count: int
    created_at: datetime
    deleted_at: datetime | None

    model_config = {"from_attributes": True}


class PaginatedLinksResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[LinkResponse]

