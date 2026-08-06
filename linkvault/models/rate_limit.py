from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func, Index
from sqlalchemy.orm import Mapped, mapped_column

from linkvault.database import Base


class RateLimitState(Base):
    __tablename__ = "rate_limit_state"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    ip_address: Mapped[str | None] = mapped_column(
        String(45), nullable=True, index=True
    )
    request_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    window_start: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), default=datetime.utcnow
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        Index("ix_rate_limit_state_user_id_window_start", "user_id", "window_start"),
        Index("ix_rate_limit_state_ip_address_window_start", "ip_address", "window_start"),
    )
