"""Expired link cleanup job — soft-deletes links that have reached their expiry or max_clicks."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.models.link import Link

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return current UTC time as a naive datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def cleanup_expired_links(db: AsyncSession) -> None:
    """
    Soft-delete links that have:
    - Passed their `expires_at` time, OR
    - Reached their `max_clicks` limit

    Emits structured JSON log lines for each deletion operation.
    """
    now = _utc_now()

    try:
        # ---- Find expired links by expiry time ----
        expired_by_time_result = await db.execute(
            select(Link).where(
                and_(
                    Link.expires_at.isnot(None),
                    Link.expires_at <= now,
                    Link.deleted_at.is_(None),
                )
            )
        )
        expired_by_time = expired_by_time_result.scalars().all()

        # ---- Find expired links by max_clicks ----
        expired_by_clicks_result = await db.execute(
            select(Link).where(
                and_(
                    Link.max_clicks.isnot(None),
                    Link.click_count >= Link.max_clicks,
                    Link.deleted_at.is_(None),
                )
            )
        )
        expired_by_clicks = expired_by_clicks_result.scalars().all()

        # Deduplicate in case a link matches both criteria
        all_links_to_delete = {link.id: link for link in expired_by_time}
        for link in expired_by_clicks:
            all_links_to_delete[link.id] = link

        if not all_links_to_delete:
            logger.info(
                json.dumps(
                    {
                        "event": "cleanup",
                        "status": "success",
                        "timestamp": now.isoformat(),
                        "links_deleted": 0,
                    }
                )
            )
            return

        # ---- Soft-delete all expired links ----
        link_ids = list(all_links_to_delete.keys())
        await db.execute(
            update(Link)
            .where(Link.id.in_(link_ids))
            .values(deleted_at=now)
        )
        await db.commit()

        logger.info(
            json.dumps(
                {
                    "event": "cleanup",
                    "status": "success",
                    "timestamp": now.isoformat(),
                    "links_deleted": len(all_links_to_delete),
                    "reason_breakdown": {
                        "expired_by_time": len(expired_by_time),
                        "expired_by_clicks": len(expired_by_clicks),
                    },
                }
            )
        )

    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "cleanup",
                    "status": "error",
                    "timestamp": _utc_now().isoformat(),
                    "error": str(exc),
                }
            )
        )
        raise
