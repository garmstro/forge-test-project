"""Expired link cleanup job — Item 5.

Runs every 15 minutes via APScheduler. Marks expired links as deleted
(soft-delete) by setting deleted_at to the current timestamp.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from linkvault.config import settings
from linkvault.models.link import Link

logger = logging.getLogger(__name__)


def expire_links(db_url: str | None = None) -> None:
    """
    APScheduler job function to mark expired links as deleted.

    Must use synchronous SQLAlchemy because APScheduler executes jobs
    in a thread pool, not an async context.

    Logs a structured JSON message with the number of links expired.
    
    Args:
        db_url: Optional database URL. If not provided, uses settings.DATABASE_URL.
                This is primarily for testing with in-memory databases.
    """
    try:
        # Use provided db_url or fall back to settings
        database_url = db_url or settings.DATABASE_URL
        
        # Convert async URL to sync URL if needed
        sync_url = database_url.replace("aiosqlite", "sqlite")
        
        # Create synchronous engine for APScheduler thread pool context
        engine = create_engine(
            sync_url,
            echo=False,
        )

        with Session(engine) as session:
            now = datetime.utcnow()

            # Find all links that have expired but haven't been deleted yet
            stmt = select(Link).where(
                Link.expires_at < now,
                Link.deleted_at.is_(None),
            )
            expired_links = session.execute(stmt).scalars().all()

            if expired_links:
                # Mark all expired links as deleted
                update_stmt = (
                    update(Link)
                    .where(
                        Link.expires_at < now,
                        Link.deleted_at.is_(None),
                    )
                    .values(deleted_at=now)
                )
                session.execute(update_stmt)
                session.commit()

            links_expired = len(expired_links)

            # Emit structured JSON log
            log_entry = {
                "event": "cleanup",
                "links_expired": links_expired,
                "timestamp": now.isoformat() + "Z",
            }
            logger.info(json.dumps(log_entry))

        engine.dispose()

    except Exception as exc:
        logger.exception("Cleanup job failed: %s", exc)
