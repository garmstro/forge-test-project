from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from linkvault.database import Base


class RateLimitState(Base):
    """
    Tracks rate limit state for users and IP addresses.
    
    Each row represents a rate limit window for a specific user_id or ip_address.
    The window_start timestamp marks when the current rate limit window began.
    request_count tracks requests within the current window.
    """
    __tablename__ = "rate_limit_state"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # Either user_id or ip_address is set, but not both
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True, index=True)
    
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_start: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), default=datetime.utcnow, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), default=datetime.utcnow, onupdate=datetime.utcnow
    )
