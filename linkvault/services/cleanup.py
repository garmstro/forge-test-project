"""Expired link cleanup job — runs every 15 minutes via APScheduler."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.models.link import Link

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return the current UTC time as a naive datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def cleanup_expired_links(db: AsyncSession) -> None:
    """
    Find all links where expires_at < now AND deleted_at IS NULL,
    set deleted_at = now, and log structured JSON.

    This job is designed to be called by APScheduler every 15 minutes.
    
    Note: The caller is responsible for committing the session.
    """
    now = _utc_now()

    # Find all expired, non-deleted links
    stmt = select(Link).where(
        Link.expires_at < now,
        Link.deleted_at.is_(None),
    )
    result = await db.execute(stmt)
    expired_links = result.scalars().all()

    links_expired = len(expired_links)

    if links_expired > 0:
        # Mark all as deleted
        update_stmt = (
            update(Link)
            .where(
                Link.expires_at < now,
                Link.deleted_at.is_(None),
            )
            .values(deleted_at=now)
        )
        await db.execute(update_stmt)

    # Log structured JSON
    log_entry = {
        "event": "cleanup",
        "links_expired": links_expired,
        "timestamp": now.isoformat(),
    }
    logger.info(json.dumps(log_entry))
