from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from linkvault.database import Base

# SEC-07: cap stored lengths to prevent storage-exhaustion via crafted headers.
# 512 chars covers all realistic User-Agent strings; 2 048 covers all realistic
# Referer URLs.  Values exceeding these limits are truncated before storage.
_USER_AGENT_MAX = 512
_REFERER_MAX = 2048


class Click(Base):
    __tablename__ = "clicks"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    link_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("links.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # SEC-14: use timezone-aware datetime.now(timezone.utc) instead of the
    # deprecated datetime.utcnow() which returns a naive datetime and will
    # behave incorrectly in Python 3.12+.
    clicked_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        index=True,
    )
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    # SEC-07: store as bounded String rather than unbounded Text
    user_agent: Mapped[str | None] = mapped_column(String(_USER_AGENT_MAX), nullable=True)
    referer: Mapped[str | None] = mapped_column(String(_REFERER_MAX), nullable=True)

