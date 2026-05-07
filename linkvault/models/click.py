from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from linkvault.database import Base


class Click(Base):
    __tablename__ = "clicks"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    link_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("links.id", ondelete="CASCADE"), nullable=False, index=True
    )
    clicked_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), default=datetime.utcnow, index=True
    )
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    referer: Mapped[str | None] = mapped_column(Text, nullable=True)

