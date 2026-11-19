"""APScheduler initialization and lifecycle management."""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from linkvault.config import settings
from linkvault.services.cleanup import cleanup_expired_links

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: AsyncIOScheduler | None = None


async def init_scheduler(db_factory: async_sessionmaker[AsyncSession]) -> AsyncIOScheduler | None:
    """
    Initialize and start the APScheduler with a persistent SQLite job store.

    The job store is backed by SCHEDULER_DB_URL (default: sqlite:///./scheduler.db).
    This ensures jobs survive application restarts.

    Returns None if scheduler initialization is skipped (e.g., in test environments).
    """
    global _scheduler

    if _scheduler is not None:
        return _scheduler

    # Skip scheduler initialization for in-memory test databases
    if ":memory:" in settings.DATABASE_URL:
        logger.info("Skipping scheduler initialization for in-memory test database")
        return None

    # Create the job store with the configured database URL
    # Note: APScheduler's SQLAlchemy job store uses synchronous SQLAlchemy,
    # so we pass the sync URL directly (without +aiosqlite)
    job_store = SQLAlchemyJobStore(url=settings.SCHEDULER_DB_URL)

    scheduler = AsyncIOScheduler(
        jobstores={"default": job_store},
        timezone="UTC",
    )

    # Define the cleanup job
    async def cleanup_job() -> None:
        """Wrapper to get a fresh async session and call the cleanup service."""
        async with db_factory() as session:
            await cleanup_expired_links(session)

    # Add the job: run every 15 minutes
    scheduler.add_job(
        cleanup_job,
        "interval",
        minutes=15,
        id="cleanup_expired_links",
        name="Cleanup expired links",
        replace_existing=True,  # Replace if already scheduled (e.g., on app restart)
    )

    try:
        scheduler.start()
        _scheduler = scheduler
        logger.info("APScheduler started with cleanup job (every 15 minutes)")
    except Exception as e:
        logger.error("Failed to start APScheduler: %s", e)
        raise

    return scheduler


async def shutdown_scheduler() -> None:
    """Gracefully shut down the scheduler."""
    global _scheduler

    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=True)
        except Exception as e:
            logger.error("Error shutting down scheduler: %s", e)
        finally:
            _scheduler = None
            logger.info("APScheduler shut down")


def get_scheduler() -> AsyncIOScheduler | None:
    """Return the global scheduler instance, or None if not initialized."""
    return _scheduler
