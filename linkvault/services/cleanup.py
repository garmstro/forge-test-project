"""Expired link cleanup job (spec item 5).

Every 15 minutes, every link whose ``expires_at`` is in the past and which is
not already soft-deleted gets ``deleted_at = now``. Each run writes one
structured JSON log line::

    {"event": "cleanup", "links_expired": N, "timestamp": "2025-01-01T00:00:00Z"}

The job runs on an APScheduler ``AsyncIOScheduler`` whose job store is a
SQLite database (``settings.SCHEDULER_DB_URL``), so the schedule survives
application restarts. On startup the job is only added if it isn't already in
the store, so a restart keeps the stored ``next_run_time``. Runs missed while
the app was down are merged into one catch-up run when it starts again.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.config import settings
from linkvault.models.link import Link

logger = logging.getLogger("linkvault.cleanup")

CLEANUP_JOB_ID = "expired_link_cleanup"
CLEANUP_INTERVAL_MINUTES = 15
# Textual reference so the persistent job store never has to pickle a function.
CLEANUP_JOB_FUNC = "linkvault.services.cleanup:run_cleanup_job"


def _utc_now() -> datetime:
    """Naive UTC 'now', matching how datetimes are stored in the database."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def purge_expired_links(session: AsyncSession, now: datetime | None = None) -> int:
    """Soft-delete every expired, still-active link. Returns how many were changed.

    This is a single bulk ``UPDATE``. It does not commit; the caller owns the
    transaction.
    """
    now = now or _utc_now()
    stmt = (
        update(Link)
        .where(Link.expires_at.is_not(None))
        .where(Link.expires_at < now)
        .where(Link.deleted_at.is_(None))
        .values(deleted_at=now)
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def log_cleanup_result(links_expired: int, now: datetime) -> str:
    """Emit the structured cleanup log line and return it (handy for tests)."""
    line = json.dumps(
        {
            "event": "cleanup",
            "links_expired": links_expired,
            "timestamp": now.replace(microsecond=0).isoformat() + "Z",
        }
    )
    logger.info(line)
    return line


async def run_cleanup(session: AsyncSession) -> int:
    """Run one cleanup pass on ``session``, commit, and log the result."""
    now = _utc_now()
    count = await purge_expired_links(session, now)
    await session.commit()
    log_cleanup_result(count, now)
    return count


async def run_cleanup_job() -> int:
    """Entry point APScheduler calls. Opens its own database session."""
    # Imported here so tests can swap the engine before the job runs.
    from linkvault.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        try:
            return await run_cleanup(session)
        except Exception:
            await session.rollback()
            logger.exception("Expired link cleanup failed")
            raise


def create_scheduler(jobstore_url: str | None = None) -> AsyncIOScheduler:
    """Build an (unstarted) scheduler backed by a persistent SQLAlchemy job store."""
    return AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=jobstore_url or settings.SCHEDULER_DB_URL)},
        job_defaults={
            "coalesce": True,  # collapse missed runs into one
            "max_instances": 1,  # never overlap two cleanups
            "misfire_grace_time": None,  # always run a missed cleanup, however late
        },
        timezone=timezone.utc,
    )


def start_scheduler(scheduler: AsyncIOScheduler) -> None:
    """Start the scheduler and register the cleanup job if it isn't stored yet.

    Must be called while an asyncio event loop is running (e.g. in the FastAPI
    lifespan).
    """
    scheduler.start()
    if scheduler.get_job(CLEANUP_JOB_ID) is None:
        scheduler.add_job(
            CLEANUP_JOB_FUNC,
            trigger="interval",
            minutes=CLEANUP_INTERVAL_MINUTES,
            id=CLEANUP_JOB_ID,
            name="Expired link cleanup",
            replace_existing=True,
        )
        logger.info("Registered cleanup job %r (every %d min)", CLEANUP_JOB_ID, CLEANUP_INTERVAL_MINUTES)
    else:
        logger.info("Resumed persisted cleanup job %r", CLEANUP_JOB_ID)


def shutdown_scheduler(scheduler: AsyncIOScheduler) -> None:
    """Stop the scheduler without waiting for a running job to finish."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
