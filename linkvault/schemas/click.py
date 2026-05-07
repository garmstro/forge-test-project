from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ClickResponse(BaseModel):
    id: str
    link_id: str
    clicked_at: datetime
    ip_address: str | None
    user_agent: str | None
    referer: str | None

    model_config = {"from_attributes": True}

