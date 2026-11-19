"""APScheduler initialization and job management."""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from linkvault.config import settings
from linkvault.database import AsyncSessionLocal
from linkvault.services.cleanup import cleanup_expired_links

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: AsyncIOScheduler | None = None


async def init_scheduler() -> AsyncIOScheduler:
    """
    Initialize and start the APScheduler with a persistent SQLite job store.

    Returns the scheduler instance.
    """
    global _scheduler

    if _scheduler is not None:
        return _scheduler

    # Create job store backed by SQLite (non-async URL for SQLAlchemy job store)
    job_store = SQLAlchemyJobStore(url=settings.SCHEDULER_DB_URL)

    # Create scheduler with the job store
    _scheduler = AsyncIOScheduler(
        jobstores={"default": job_store},
        timezone="UTC",
    )

    # Add the cleanup job: run every 15 minutes
    _scheduler.add_job(
        _cleanup_job_wrapper,
        "interval",
        minutes=15,
        id="cleanup_expired_links",
        name="Cleanup expired links",
        replace_existing=True,
        max_instances=1,  # Prevent concurrent runs
    )

    # Start the scheduler
    _scheduler.start()
    logger.info("APScheduler started with cleanup job (every 15 minutes)")

    return _scheduler


async def _cleanup_job_wrapper() -> None:
    """
    Wrapper for the cleanup job that creates a fresh async session.

    APScheduler calls this periodically; we need to create a new session
    for each invocation.
    """
    async with AsyncSessionLocal() as session:
        try:
            await cleanup_expired_links(session)
        except Exception as e:
            logger.exception("Error in cleanup job: %s", e)


async def shutdown_scheduler() -> None:
    """Shutdown the scheduler gracefully."""
    global _scheduler

    if _scheduler is not None:
        _scheduler.shutdown(wait=True)
        _scheduler = None
        logger.info("APScheduler shut down")


def get_scheduler() -> AsyncIOScheduler | None:
    """Return the current scheduler instance, or None if not initialized."""
    return _scheduler
