"""Expired link cleanup job and the background scheduler that runs it.

Every ``CLEANUP_INTERVAL_MINUTES`` the scheduler runs :func:`run_cleanup_job`,
which soft-deletes every link whose ``expires_at`` is in the past and whose
``deleted_at`` is still NULL.  Each run emits one structured log line::

    {"event": "cleanup", "links_expired": N, "timestamp": "..."}

The schedule lives in a persistent SQLAlchemy job store (``SCHEDULER_DB_URL``)
so it survives application restarts.  All times are UTC; like the rest of the
app, datetimes are stored naive and interpreted as UTC.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from apscheduler.job import Job
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import STATE_RUNNING
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from linkvault.config import settings
from linkvault.database import AsyncSessionLocal
from linkvault.models.link import Link

logger = logging.getLogger(__name__)

CLEANUP_JOB_ID = "expired_link_cleanup"
CLEANUP_INTERVAL_MINUTES = 15
# Textual reference so the persisted job never depends on pickling a function.
CLEANUP_JOB_FUNC_REF = "linkvault.services.cleanup:run_cleanup_job"

_scheduler: AsyncIOScheduler | None = None


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Current UTC time as a naive datetime (the app's storage convention)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def expire_links(session: AsyncSession, now: datetime | None = None) -> int:
    """Soft-delete every expired, not-yet-deleted link in a single UPDATE.

    Returns the number of links that were marked as deleted.  The caller is
    responsible for committing the session.
    """
    now = now if now is not None else _utcnow()
    # ``expires_at < now`` is never true for NULL, so links without an expiry
    # are left alone; ``deleted_at IS NULL`` preserves earlier deletion times.
    stmt = (
        update(Link)
        .where(Link.expires_at < now, Link.deleted_at.is_(None))
        .values(deleted_at=now)
        .execution_options(synchronize_session=False)
    )
    # DML statements always yield a CursorResult, which carries rowcount.
    result = cast(CursorResult[Any], await session.execute(stmt))
    return int(result.rowcount)


async def run_cleanup_job(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int | None:
    """Scheduler entry point: expire links, commit, and log the outcome.

    Never raises — a failure is logged and the scheduler simply carries on
    to the next run.  Returns the number of links expired, or ``None`` if the
    run failed.
    """
    factory = session_factory if session_factory is not None else AsyncSessionLocal
    now = _utcnow()
    timestamp = now.replace(tzinfo=timezone.utc).isoformat()

    try:
        async with factory() as session:
            count = await expire_links(session, now)
            await session.commit()
    except Exception as exc:
        logger.error(
            json.dumps(
                {"event": "cleanup_failed", "error": str(exc), "timestamp": timestamp}
            ),
            exc_info=True,
        )
        return None

    logger.info(
        json.dumps({"event": "cleanup", "links_expired": count, "timestamp": timestamp})
    )
    return count


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------


def _has_expected_schedule(job: Job) -> bool:
    trigger = job.trigger
    return (
        job.func_ref == CLEANUP_JOB_FUNC_REF
        and isinstance(trigger, IntervalTrigger)
        and trigger.interval == timedelta(minutes=CLEANUP_INTERVAL_MINUTES)
    )


def _ensure_cleanup_job(scheduler: AsyncIOScheduler) -> Job:
    """Register the cleanup job exactly once.

    If the persistent store already holds a matching job (e.g. after a
    restart) it is kept as-is so its saved next run time is honoured;
    otherwise the job is (re)created, replacing any stale definition.
    """
    existing = scheduler.get_job(CLEANUP_JOB_ID)
    if existing is not None and _has_expected_schedule(existing):
        logger.info(
            "Cleanup job already scheduled; next run at %s", existing.next_run_time
        )
        return existing

    return scheduler.add_job(
        CLEANUP_JOB_FUNC_REF,
        trigger=IntervalTrigger(minutes=CLEANUP_INTERVAL_MINUTES, timezone=timezone.utc),
        id=CLEANUP_JOB_ID,
        name="Expired link cleanup",
        replace_existing=True,
        coalesce=True,  # after downtime, catch up with one run, not many
        max_instances=1,
        misfire_grace_time=None,  # a late run is still worth doing
    )


def start_scheduler(jobstore_url: str | None = None) -> AsyncIOScheduler:
    """Start the background scheduler with a persistent job store.

    Must be called from within a running event loop (e.g. the app lifespan).
    Calling it while the scheduler is already running is a no-op.
    """
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    url = jobstore_url if jobstore_url is not None else settings.SCHEDULER_DB_URL
    scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=url)},
        timezone=timezone.utc,
    )
    # Start paused so the job store is loaded before we inspect/register the
    # job, and nothing runs until registration is complete.
    scheduler.start(paused=True)
    try:
        _ensure_cleanup_job(scheduler)
        scheduler.resume()
    except Exception:
        scheduler.shutdown(wait=False)
        raise

    _scheduler = scheduler
    logger.info(
        "Scheduler started; cleanup job runs every %d minutes",
        CLEANUP_INTERVAL_MINUTES,
    )
    return scheduler


async def shutdown_scheduler() -> None:
    """Stop the scheduler, if running, and release its job store."""
    global _scheduler
    scheduler = _scheduler
    _scheduler = None
    if scheduler is None or not scheduler.running:
        return

    scheduler.shutdown(wait=False)
    # AsyncIOScheduler performs the actual shutdown via a callback on the
    # event loop; yield so it completes before the loop goes away.
    await asyncio.sleep(0)
    logger.info("Scheduler stopped")


def get_scheduler() -> AsyncIOScheduler | None:
    """Return the active scheduler instance, if any."""
    return _scheduler


def is_scheduler_running() -> bool:
    """True while the background scheduler is started and processing jobs."""
    return _scheduler is not None and _scheduler.state == STATE_RUNNING
