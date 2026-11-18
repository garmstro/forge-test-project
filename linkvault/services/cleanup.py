"""Expired link cleanup job — soft-deletes links past their expiration date."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.models.link import Link

logger = logging.getLogger(__name__)


async def cleanup_expired_links(db_session: AsyncSession) -> None:
    """
    Soft-delete all expired links by setting deleted_at to now (UTC).
    
    Queries links where expires_at < now AND deleted_at IS NULL, then sets
    deleted_at = now (UTC) on those links. Logs a structured JSON message
    with the count of expired links.
    
    Args:
        db_session: Async database session for executing queries.
    """
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        # Find all expired links that haven't been deleted yet
        stmt = select(Link.id).where(
            Link.expires_at < now,
            Link.deleted_at.is_(None),
        )
        
        result = await db_session.execute(stmt)
        expired_ids = [row[0] for row in result.fetchall()]
        
        if expired_ids:
            # Soft-delete all expired links
            update_stmt = (
                update(Link)
                .where(Link.id.in_(expired_ids))
                .values(deleted_at=now)
            )
            await db_session.execute(update_stmt)
            await db_session.commit()
        
        # Log structured JSON message
        log_message = json.dumps({
            "event": "cleanup",
            "links_expired": len(expired_ids),
            "timestamp": now.isoformat(),
        })
        logger.info(log_message)
        
    except Exception:
        logger.exception("Error during expired link cleanup")
        await db_session.rollback()
        raise


__all__ = ["cleanup_expired_links"]
