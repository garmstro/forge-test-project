"""Expired link cleanup job.

Every 15 minutes a background job soft-deletes links whose ``expires_at`` is
in the past.  This is the same soft-delete used by ``DELETE /links/{slug}``:
``deleted_at`` is set, the slug becomes reusable, and click history is kept
(clicks are tied to the link's UUID, not its slug — see DECISIONS.md §3).

Links capped by ``max_clicks`` are *not* removed by this job; they keep
returning ``410 Gone`` until the owner deletes or edits them.

The job is registered with an APScheduler ``AsyncIOScheduler`` backed by a
persistent SQLAlchemy job store (``SCHEDULER_DB_URL``) so the schedule
survives application restarts.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import cast

from apscheduler.job import Job
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from linkvault.database import AsyncSessionLocal
from linkvault.models.link import Link

logger = logging.getLogger(__name__)

CLEANUP_JOB_ID = "expired_link_cleanup"
CLEANUP_INTERVAL = timedelta(minutes=15)
# Textual reference stored in the persistent job store.
CLEANUP_JOB_REF = "linkvault.services.cleanup:cleanup_expired_links_job"


def _utcnow() -> datetime:
    """Current time as a *naive* UTC datetime.

    The app stores all datetimes as naive UTC (see DECISIONS.md §4), so
    comparisons must use the same representation.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso_utc(value: datetime) -> str:
    return value.replace(tzinfo=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Core cleanup logic
# ---------------------------------------------------------------------------


async def expire_links(session: AsyncSession, now: datetime | None = None) -> int:
    """Soft-delete every active link whose ``expires_at`` is before *now*.

    Uses a single ``UPDATE … WHERE`` statement so there is no read-then-write
    race with concurrent requests.  Links with no expiry and links that are
    already deleted are never touched.  The caller is responsible for
    committing the session.

    Returns the number of links that were expired.
    """
    if now is None:
        now = _utcnow()
    result = await session.execute(
        update(Link)
        .where(
            Link.expires_at.is_not(None),
            Link.expires_at < now,
            Link.deleted_at.is_(None),
        )
        .values(deleted_at=now)
        .returning(Link.id)
    )
    return len(result.scalars().all())


async def run_cleanup(
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    now: datetime | None = None,
) -> int:
    """Run one cleanup pass in its own transaction and log the result.

    Emits a structured log line on every run, including when nothing expired::

        {"event": "cleanup", "links_expired": N, "timestamp": "..."}
    """
    if now is None:
        now = _utcnow()
    async with session_factory() as session:
        try:
            count = await expire_links(session, now)
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    logger.info(
        json.dumps(
            {"event": "cleanup", "links_expired": count, "timestamp": _iso_utc(now)}
        )
    )
    return count


async def cleanup_expired_links_job() -> None:
    """Scheduler entry point.

    Failures are logged and swallowed so a single bad run never crashes the
    application; the scheduler simply tries again at the next interval.
    """
    try:
        await run_cleanup()
    except Exception as exc:
        logger.exception(
            json.dumps(
                {
                    "event": "cleanup_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "timestamp": _iso_utc(_utcnow()),
                }
            )
        )


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------


def create_scheduler(jobstore_url: str) -> AsyncIOScheduler:
    """Build an AsyncIOScheduler whose jobs persist in *jobstore_url*."""
    return AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=jobstore_url)},
        timezone=timezone.utc,
    )


async def stop_scheduler(scheduler: AsyncIOScheduler) -> None:
    """Shut *scheduler* down and wait until it has actually stopped.

    ``AsyncIOScheduler.shutdown()`` only *schedules* the shutdown on the event
    loop; yielding once lets it run, so job stores are closed before we return.
    Jobs already in progress are not interrupted.
    """
    if scheduler.running:
        scheduler.shutdown(wait=False)
        await asyncio.sleep(0)


def _job_is_current(job: Job) -> bool:
    trigger = job.trigger
    return (
        job.func_ref == CLEANUP_JOB_REF
        and isinstance(trigger, IntervalTrigger)
        and trigger.interval == CLEANUP_INTERVAL
    )


def ensure_cleanup_job(scheduler: AsyncIOScheduler) -> Job:
    """Register the cleanup job exactly once.

    Must be called after ``scheduler.start()`` so the persistent store is
    open.  If a matching job is already stored (e.g. after a restart) it is
    left alone, keeping its existing ``next_run_time`` so frequent restarts
    don't keep pushing the next run back.  An outdated definition is
    replaced in place under the same id — never duplicated.
    """
    existing = cast("Job | None", scheduler.get_job(CLEANUP_JOB_ID))
    if existing is not None and _job_is_current(existing):
        logger.info("Cleanup job already scheduled (next run %s)", existing.next_run_time)
        return existing

    job = cast(
        Job,
        scheduler.add_job(
            CLEANUP_JOB_REF,
            trigger=IntervalTrigger(seconds=int(CLEANUP_INTERVAL.total_seconds())),
            id=CLEANUP_JOB_ID,
            name="Expired link cleanup",
            replace_existing=True,
            # After downtime, run once to catch up rather than once per missed slot.
            coalesce=True,
            misfire_grace_time=None,
            max_instances=1,
        ),
    )
    logger.info(
        "Cleanup job scheduled every %s (next run %s)", CLEANUP_INTERVAL, job.next_run_time
    )
    return job
