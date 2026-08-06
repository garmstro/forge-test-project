from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from linkvault.database import Base


class RateLimitState(Base):
    """Tracks rate limit state per user/IP for persistence across server restarts."""

    __tablename__ = "rate_limit_state"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # Either user_id or ip_address will be set, depending on the rate limit key
    user_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    ip_address: Mapped[str | None] = mapped_column(
        String(45), nullable=True, index=True
    )
    # Request counts for different time windows
    requests_this_minute: Mapped[int] = mapped_column(Integer, default=0)
    requests_this_hour: Mapped[int] = mapped_column(Integer, default=0)
    requests_this_day: Mapped[int] = mapped_column(Integer, default=0)
    # Timestamps for window resets
    minute_window_reset_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    hour_window_reset_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    day_window_reset_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), default=datetime.utcnow
    )
