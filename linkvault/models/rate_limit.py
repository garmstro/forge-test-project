from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from linkvault.database import Base


class RateLimitStore(Base):
    """Track request counts per user or IP address for rate limiting.
    
    Entries are keyed by either:
    - user_id (for authenticated requests)
    - ip_address (for public/unauthenticated requests)
    
    The window_start marks the beginning of the current rate limit window.
    Entries older than the configured window duration are considered expired
    and can be cleaned up.
    """

    __tablename__ = "rate_limit_store"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # Either user_id or ip_address is set, but not both
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True, index=True)
    
    # Request count within the current window
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    
    # Start of the current rate limit window
    window_start: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), default=datetime.utcnow, index=True
    )
    
    # When this record was created
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), default=datetime.utcnow
    )
