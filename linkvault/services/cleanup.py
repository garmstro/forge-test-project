"""Expired link cleanup job.

Every :data:`CLEANUP_INTERVAL_MINUTES` minutes the background scheduler
marks every link whose ``expires_at`` lies in the past (and which is not
already deleted) as soft-deleted by setting ``deleted_at`` to the current
UTC time.  Rows are never physically removed, so click history is kept,
and the slug becomes free for reuse (see DECISIONS.md §7).

The schedule itself is persisted in the APScheduler job store backed by
``settings.SCHEDULER_DB_URL`` so it survives application restarts and is
registered exactly once.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import STATE_RUNNING
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.database import AsyncSessionLocal
from linkvault.models.link import Link

logger = logging.getLogger(__name__)

CLEANUP_JOB_ID = "expired_link_cleanup"
CLEANUP_INTERVAL_MINUTES = 15

# Module-level handle on the running scheduler (None until started).
_scheduler: AsyncIOScheduler | None = None


# ---------------------------------------------------------------------------
# Cleanup logic
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    """Naive UTC "now" — matches how datetimes are stored in the links table."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def cleanup_expired_links(db: AsyncSession, now: datetime | None = None) -> int:
    """Soft-delete every expired, not-yet-deleted link and commit.

    A link is expired when ``expires_at < now``.  Links without an expiry,
    with a future expiry, or that are already soft-deleted are untouched
    (an existing ``deleted_at`` is never overwritten).

    Emits one structured log line per call — even when nothing expired —
    and returns the number of links marked deleted.
    """
    if now is None:
        now = _utc_now()

    result = await db.execute(
        update(Link)
        .where(
            Link.expires_at.is_not(None),
            Link.expires_at < now,
            Link.deleted_at.is_(None),
        )
        .values(deleted_at=now)
        .execution_options(synchronize_session="fetch")
    )
    await db.commit()

    links_expired = int(result.rowcount or 0)  # type: ignore[attr-defined]
    logger.info(
        json.dumps(
            {
                "event": "cleanup",
                "links_expired": links_expired,
                "timestamp": now.replace(tzinfo=timezone.utc).isoformat(),
            }
        )
    )
    return links_expired


async def run_cleanup_job() -> None:
    """Scheduler entry point: run one cleanup pass in its own DB session.

    Must stay a module-level function — the persistent job store refers to
    it by its import path (``linkvault.services.cleanup:run_cleanup_job``).
    """
    async with AsyncSessionLocal() as session:
        try:
            await cleanup_expired_links(session)
        except Exception:
            await session.rollback()
            logger.exception("Expired link cleanup failed")
            raise


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------


def _has_expected_trigger(trigger: object) -> bool:
    return isinstance(trigger, IntervalTrigger) and trigger.interval == timedelta(
        minutes=CLEANUP_INTERVAL_MINUTES
    )


def start_scheduler(jobstore_url: str) -> AsyncIOScheduler:
    """Start the background scheduler and ensure the cleanup job is registered.

    Must be called from within a running asyncio event loop (e.g. the
    FastAPI lifespan).  If the job already exists in the persistent store
    (from a previous run) it is kept as-is so its next run time carries
    over the restart; it is only (re)scheduled if missing or if its
    interval no longer matches.
    """
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=jobstore_url)},
        job_defaults={
            # If the app was down when a run was due, run once on startup
            # instead of skipping or replaying every missed run.
            "coalesce": True,
            "misfire_grace_time": None,
            "max_instances": 1,
        },
        timezone=timezone.utc,
    )
    scheduler.start()

    existing = scheduler.get_job(CLEANUP_JOB_ID)
    if existing is None:
        scheduler.add_job(
            run_cleanup_job,
            trigger=IntervalTrigger(minutes=CLEANUP_INTERVAL_MINUTES),
            id=CLEANUP_JOB_ID,
            name="Expired link cleanup",
            replace_existing=True,
        )
        logger.info("Registered cleanup job (every %d minutes)", CLEANUP_INTERVAL_MINUTES)
    elif not _has_expected_trigger(existing.trigger):
        scheduler.reschedule_job(
            CLEANUP_JOB_ID, trigger=IntervalTrigger(minutes=CLEANUP_INTERVAL_MINUTES)
        )
        logger.info("Rescheduled cleanup job (every %d minutes)", CLEANUP_INTERVAL_MINUTES)
    else:
        logger.info("Cleanup job already scheduled; next run at %s", existing.next_run_time)

    _scheduler = scheduler
    return scheduler


async def shutdown_scheduler() -> None:
    """Stop the scheduler (if running) and release its job store connection."""
    scheduler = _scheduler
    if scheduler is None or not scheduler.running:
        return
    scheduler.shutdown()
    # AsyncIOScheduler defers the actual shutdown to the next loop iteration;
    # yield once so it completes before the app finishes shutting down.
    await asyncio.sleep(0)


def get_scheduler() -> AsyncIOScheduler | None:
    """Return the scheduler instance started by :func:`start_scheduler`, if any."""
    return _scheduler


def scheduler_status() -> str:
    """Return ``running``, ``not_started`` or ``stopped`` for the health check."""
    scheduler = _scheduler
    if scheduler is None:
        return "not_started"
    if scheduler.state == STATE_RUNNING:
        return "running"
    return "stopped"
