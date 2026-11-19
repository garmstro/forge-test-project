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
    """Return current time as a naive UTC datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def cleanup_expired_links(db: AsyncSession) -> None:
    """Find expired links and mark them with deleted_at = now.

    A link is expired if:
      - expires_at < now AND
      - deleted_at IS NULL

    After cleanup, the slug becomes immediately reusable.
    """
    now = _utc_now()

    # Find all expired, non-deleted links
    select_stmt = select(Link).where(
        Link.expires_at < now,
        Link.deleted_at.is_(None),
    )
    result = await db.execute(select_stmt)
    expired_links = result.scalars().all()

    if not expired_links:
        logger.info(json.dumps({"event": "cleanup", "links_expired": 0, "timestamp": now.isoformat()}))
        return

    # Mark them as deleted
    link_ids = [link.id for link in expired_links]
    update_stmt = update(Link).where(Link.id.in_(link_ids)).values(deleted_at=now)
    await db.execute(update_stmt)
    await db.commit()

    count = len(expired_links)
    logger.info(
        json.dumps({
            "event": "cleanup",
            "links_expired": count,
            "timestamp": now.isoformat(),
        })
    )
