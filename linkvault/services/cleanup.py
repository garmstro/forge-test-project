"""Expired link cleanup job.

Every ``CLEANUP_INTERVAL_MINUTES`` minutes an APScheduler job soft-deletes
links whose ``expires_at`` has passed (``deleted_at`` is set to *now*).  The
job is stored in a SQLite-backed job store (``settings.SCHEDULER_DB_URL``) so
that it survives application restarts.

Naive datetimes are treated as UTC, matching the rest of the application.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore  # type: ignore[import-untyped]
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from linkvault.config import settings
from linkvault.database import AsyncSessionLocal
from linkvault.models.link import Link

logger = logging.getLogger(__name__)

CLEANUP_INTERVAL_MINUTES = 15
CLEANUP_JOB_ID = "expired_link_cleanup"


async def expire_stale_links(
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    now: datetime | None = None,
) -> int:
    """Soft-delete all active links whose ``expires_at`` is in the past.

    Returns the number of links expired and emits one structured JSON log line.
    """
    current = now if now is not None else datetime.utcnow()
    async with session_factory() as session:
        result = await session.execute(
            update(Link)
            .where(Link.expires_at.is_not(None))
            .where(Link.expires_at < current)
            .where(Link.deleted_at.is_(None))
            .values(deleted_at=current)
        )
        await session.commit()
        # rowcount is available on CursorResult returned by DML statements.
        expired = int(getattr(result, "rowcount", 0) or 0)

    logger.info(
        json.dumps(
            {
                "event": "cleanup",
                "links_expired": expired,
                "timestamp": current.isoformat() + "Z",
            }
        )
    )
    return expired


async def run_cleanup_job() -> None:
    """Scheduler entry point.

    Must stay a module-level function so the persistent job store can
    reference it by its import path.  Errors are logged, never raised, so a
    failed run does not affect later runs.
    """
    try:
        await expire_stale_links()
    except Exception:
        logger.exception("Expired link cleanup failed")


def create_scheduler(job_store_url: str | None = None) -> AsyncIOScheduler:
    """Build a scheduler with a persistent SQLite job store and the cleanup job."""
    scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=job_store_url or settings.SCHEDULER_DB_URL)},
        timezone="UTC",
    )
    # replace_existing keeps a single job across restarts (the persisted one
    # is overwritten, so interval changes in code take effect).
    scheduler.add_job(
        run_cleanup_job,
        trigger="interval",
        minutes=CLEANUP_INTERVAL_MINUTES,
        id=CLEANUP_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    return scheduler
