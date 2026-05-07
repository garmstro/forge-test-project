from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from linkvault.database import Base


class Link(Base):
    __tablename__ = "links"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # No DB-level UNIQUE on slug — uniqueness among *active* links is enforced
    # in the application layer (filtered on deleted_at IS NULL).  A DB-level
    # unique constraint would prevent slug reuse after a soft-delete.
    slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    destination_url: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    max_clicks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    click_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), default=datetime.utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

