"""Expired link cleanup job — marks stale links as deleted."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.models.link import Link

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return current UTC time as a naive datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def cleanup_expired_links(db: AsyncSession) -> None:
    """
    Find all links where expires_at < now and deleted_at IS NULL,
    then set deleted_at = now on those links.

    Logs structured JSON: {"event": "cleanup", "links_expired": N, "timestamp": "..."}
    """
    now = _utc_now()

    # Find expired links that haven't been soft-deleted yet
    result = await db.execute(
        select(Link).where(
            Link.expires_at < now,
            Link.deleted_at.is_(None),
        )
    )
    expired_links = result.scalars().all()
    count = len(expired_links)

    if count > 0:
        # Mark all expired links as deleted
        await db.execute(
            update(Link)
            .where(
                Link.expires_at < now,
                Link.deleted_at.is_(None),
            )
            .values(deleted_at=now)
        )
        await db.commit()

    # Log structured JSON
    log_entry = {
        "event": "cleanup",
        "links_expired": count,
        "timestamp": now.isoformat(),
    }
    logger.info(json.dumps(log_entry))
