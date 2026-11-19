"""Expired link cleanup job — runs every 15 minutes via APScheduler."""
from __future__ import annotations

import json
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from sqlalchemy import select, update

from linkvault.config import settings
from linkvault.database import AsyncSessionLocal
from linkvault.models.link import Link

logger = logging.getLogger(__name__)


async def cleanup_expired_links() -> None:
    """
    Clean up expired links by setting deleted_at to the current timestamp.
    
    Queries for all links where expires_at < now AND deleted_at IS NULL,
    then updates them in a single atomic UPDATE statement.
    Logs a structured JSON message with the cleanup event.
    """
    async with AsyncSessionLocal() as session:
        try:
            now = datetime.utcnow()
            
            # Single atomic UPDATE statement: set deleted_at for expired, non-deleted links
            stmt = (
                update(Link)
                .where(Link.expires_at < now, Link.deleted_at.is_(None))
                .values(deleted_at=now)
            )
            result = await session.execute(stmt)
            await session.commit()
            
            links_expired = result.rowcount
            
            # Log structured JSON event
            log_event = {
                "event": "cleanup",
                "links_expired": links_expired,
                "timestamp": now.isoformat(),
            }
            logger.info(json.dumps(log_event))
            
        except Exception as e:
            await session.rollback()
            logger.exception("Error during expired link cleanup: %s", e)
            raise


def setup_scheduler() -> AsyncIOScheduler:
    """
    Initialize APScheduler with a persistent SQLite job store.
    
    Returns an AsyncIOScheduler configured with:
    - SQLAlchemy job store backed by settings.SCHEDULER_DB_URL
    - A job to run cleanup_expired_links every 15 minutes
    
    The scheduler is started before returning.
    """
    # Configure the SQLAlchemy job store with the scheduler database URL
    jobstore = SQLAlchemyJobStore(url=settings.SCHEDULER_DB_URL)
    
    scheduler = AsyncIOScheduler(
        jobstores={"default": jobstore},
        job_defaults={"coalesce": True, "max_instances": 1},
    )
    
    # Add the cleanup job to run every 15 minutes
    scheduler.add_job(
        cleanup_expired_links,
        "interval",
        minutes=15,
        id="cleanup_expired_links",
        replace_existing=True,
    )
    
    scheduler.start()
    logger.info(json.dumps({"event": "scheduler_started", "timestamp": datetime.utcnow().isoformat()}))
    
    return scheduler
