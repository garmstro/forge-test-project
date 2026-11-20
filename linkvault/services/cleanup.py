"""Expired link cleanup job and the APScheduler instance that runs it.

The job soft-deletes every link whose ``expires_at`` lies in the past and
that is not already deleted.  Once soft-deleted, the slug becomes reusable
(slug uniqueness is enforced only among links with ``deleted_at IS NULL``).

The scheduler uses a SQLite-backed persistent job store (``SCHEDULER_DB_URL``)
so the schedule survives application restarts.  The job is registered with a
fixed id and ``replace_existing=True`` so restarting never creates duplicates.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from linkvault.config import settings
from linkvault.models.link import Link

logger = logging.getLogger(__name__)

CLEANUP_JOB_ID = "cleanup_expired_links"
CLEANUP_INTERVAL_MINUTES = 15
# Textual reference so the persistent job store can re-import the callable.
CLEANUP_JOB_FUNC = "linkvault.services.cleanup:run_cleanup_job"

_scheduler: AsyncIOScheduler | None = None


def _utcnow() -> datetime:
    """Current time as a naive UTC datetime (matches how datetimes are stored)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def purge_expired_links(session: AsyncSession) -> int:
    """Soft-delete all expired, not-yet-deleted links in a single UPDATE.

    Commits the session, logs one structured line and returns the number of
    links that were marked as deleted.
    """
    now = _utcnow()
    stmt = (
        update(Link)
        .where(Link.expires_at.is_not(None))
        .where(Link.expires_at < now)
        .where(Link.deleted_at.is_(None))
        .values(deleted_at=now)
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    await session.commit()
    count = result.rowcount if isinstance(result, CursorResult) else 0
    count = max(count or 0, 0)
    logger.info(
        json.dumps(
            {
                "event": "cleanup",
                "links_expired": count,
                "timestamp": now.replace(tzinfo=timezone.utc).isoformat(),
            }
        )
    )
    return count


async def run_cleanup_job(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int | None:
    """Scheduler entry point.

    Opens its own session, runs the purge and never raises: a failing run is
    logged and the scheduler carries on with the next one.  Returns the number
    of expired links, or ``None`` if the run failed.
    """
    if session_factory is None:
        from linkvault.database import AsyncSessionLocal

        session_factory = AsyncSessionLocal
    try:
        async with session_factory() as session:
            return await purge_expired_links(session)
    except Exception as exc:  # noqa: BLE001 — job must never crash the scheduler
        logger.exception(
            json.dumps(
                {
                    "event": "cleanup_failed",
                    "error": str(exc),
                    "timestamp": _utcnow().replace(tzinfo=timezone.utc).isoformat(),
                }
            )
        )
        return None


def create_scheduler(jobstore_url: str | None = None) -> AsyncIOScheduler:
    """Build an AsyncIOScheduler backed by a persistent SQLite job store."""
    url = jobstore_url or settings.SCHEDULER_DB_URL
    return AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=url)},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": None},
        timezone=timezone.utc,
    )


def register_cleanup_job(scheduler: AsyncIOScheduler) -> None:
    """Add (or replace) the cleanup job; idempotent across restarts."""
    scheduler.add_job(
        CLEANUP_JOB_FUNC,
        trigger="interval",
        minutes=CLEANUP_INTERVAL_MINUTES,
        id=CLEANUP_JOB_ID,
        name="Expire links past their expires_at",
        replace_existing=True,
    )


def start_scheduler(jobstore_url: str | None = None) -> AsyncIOScheduler:
    """Create, configure and start the global scheduler (no-op if running).

    Must be called from within a running asyncio event loop.
    """
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler
    scheduler = create_scheduler(jobstore_url)
    register_cleanup_job(scheduler)
    scheduler.start()
    _scheduler = scheduler
    logger.info(json.dumps({"event": "scheduler_started", "job_id": CLEANUP_JOB_ID}))
    return scheduler


def shutdown_scheduler() -> None:
    """Stop the global scheduler if it is running."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info(json.dumps({"event": "scheduler_stopped"}))
    _scheduler = None


def is_scheduler_running() -> bool:
    """Whether the global scheduler has been started and is still running."""
    return _scheduler is not None and bool(_scheduler.running)
