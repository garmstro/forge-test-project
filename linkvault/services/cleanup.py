"""Expired link cleanup job."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore  # type: ignore[import-untyped]
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.config import settings
from linkvault.database import AsyncSessionLocal
from linkvault.models.link import Link

logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler: AsyncIOScheduler | None = None


async def expire_links_job(session: AsyncSession | None = None) -> None:
    """
    Background job that soft-deletes expired links.
    
    Runs every 15 minutes. Finds all links where:
    - expires_at < now (UTC)
    - deleted_at IS NULL
    
    Sets deleted_at = now() on those links and logs a structured message.
    
    Args:
        session: Optional AsyncSession for testing. If None, creates a new session.
    """
    now = datetime.now(timezone.utc)
    
    # Use provided session or create a new one
    should_close = session is None
    if session is None:
        session = AsyncSessionLocal()
    
    try:
        # Find expired links that haven't been deleted yet
        stmt = (
            select(Link)
            .where(Link.expires_at < now)
            .where(Link.deleted_at.is_(None))
        )
        result = await session.execute(stmt)
        expired_links = result.scalars().all()
        
        if not expired_links:
            # No links to expire - log at debug level
            logger.debug(
                json.dumps({
                    "event": "cleanup",
                    "links_expired": 0,
                    "timestamp": now.isoformat(),
                })
            )
            return
        
        # Soft-delete all expired links
        expired_ids = [link.id for link in expired_links]
        update_stmt = (
            update(Link)
            .where(Link.id.in_(expired_ids))
            .values(deleted_at=now)
        )
        await session.execute(update_stmt)
        await session.commit()
        
        # Log structured event
        logger.info(
            json.dumps({
                "event": "cleanup",
                "links_expired": len(expired_ids),
                "timestamp": now.isoformat(),
            })
        )
        
    except Exception as e:
        await session.rollback()
        logger.exception(
            json.dumps({
                "event": "cleanup_error",
                "error": str(e),
                "timestamp": now.isoformat(),
            })
        )
        raise
    finally:
        if should_close:
            await session.close()


def create_scheduler() -> AsyncIOScheduler:
    """
    Create and configure the APScheduler instance with persistent job store.
    
    Returns:
        Configured AsyncIOScheduler ready to be started.
    """
    jobstores = {
        'default': SQLAlchemyJobStore(url=settings.SCHEDULER_DB_URL)
    }
    
    scheduler_instance = AsyncIOScheduler(
        jobstores=jobstores,
        timezone='UTC',
    )
    
    return scheduler_instance


def start_scheduler() -> AsyncIOScheduler:
    """
    Start the background scheduler and register the cleanup job.
    
    The job runs every 15 minutes. If a job with the same ID already exists
    (from a previous run), it will be replaced.
    
    Returns:
        The started scheduler instance.
    """
    global scheduler
    
    if scheduler is not None:
        logger.warning("Scheduler already started")
        return scheduler
    
    scheduler = create_scheduler()
    
    # Add the cleanup job - replace if it already exists (from previous restart)
    scheduler.add_job(
        expire_links_job,
        'interval',
        minutes=15,
        id='expire_links',
        replace_existing=True,
        max_instances=1,  # Prevent overlapping executions
    )
    
    scheduler.start()
    logger.info("Scheduler started with cleanup job (runs every 15 minutes)")
    
    return scheduler


def stop_scheduler() -> None:
    """Stop the background scheduler gracefully."""
    global scheduler
    
    if scheduler is not None:
        scheduler.shutdown(wait=True)
        scheduler = None
        logger.info("Scheduler stopped")


def get_scheduler_status() -> str:
    """
    Get the current status of the scheduler.
    
    Returns:
        "running" if scheduler is active, "stopped" if not started.
    """
    if scheduler is not None and scheduler.running:
        return "running"
    return "stopped"
