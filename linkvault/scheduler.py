"""APScheduler initialization and lifecycle management."""
from __future__ import annotations

import logging
from typing import Any

from apscheduler.job import Job
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.config import settings
from linkvault.database import AsyncSessionLocal
from linkvault.services.cleanup import cleanup_expired_links

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler | None:
    """Return the global scheduler instance, or None if not started."""
    return _scheduler


async def _cleanup_job() -> None:
    """Job wrapper: acquire a session and run the cleanup task."""
    async with AsyncSessionLocal() as session:
        await cleanup_expired_links(session)


def start_scheduler() -> None:
    """Initialize and start the APScheduler instance.

    Uses a SQLite job store for persistent state across restarts.
    Schedules the cleanup job to run every 15 minutes.
    """
    global _scheduler

    if _scheduler is not None:
        logger.warning("Scheduler already started, skipping initialization.")
        return

    try:
        _scheduler = AsyncIOScheduler(
            jobstores={
                "default": {
                    "type": "sqlalchemy",
                    "url": settings.SCHEDULER_DB_URL,
                },
            },
            executors={
                "default": {
                    "type": "asyncio",
                },
            },
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
            },
        )

        # Register the cleanup job (15 minutes = 900 seconds)
        _scheduler.add_job(
            _cleanup_job,
            trigger=IntervalTrigger(seconds=900),
            id="cleanup_expired_links",
            name="Cleanup expired links",
            replace_existing=True,
        )

        _scheduler.start()
        logger.info("APScheduler started with cleanup job scheduled every 15 minutes.")

    except Exception as e:
        logger.error("Failed to start scheduler: %s", e, exc_info=True)
        raise


def stop_scheduler() -> None:
    """Stop the APScheduler instance."""
    global _scheduler

    if _scheduler is None:
        return

    try:
        _scheduler.shutdown(wait=True)
        logger.info("APScheduler stopped.")
    except Exception as e:
        logger.error("Error stopping scheduler: %s", e, exc_info=True)
    finally:
        _scheduler = None


def is_scheduler_running() -> bool:
    """Return True if the scheduler is running."""
    return _scheduler is not None and _scheduler.running
