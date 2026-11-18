"""Expired link cleanup job and the APScheduler instance that runs it.

Every run soft-deletes links whose ``expires_at`` has passed (and which are
not already deleted) by setting ``deleted_at = now``, then emits exactly one
structured JSON log line::

    {"event": "cleanup", "links_expired": N, "timestamp": "..."}

The job is scheduled every 15 minutes on an :class:`AsyncIOScheduler` whose
job store is a small SQLite database (``SCHEDULER_DB_URL``), so the schedule
survives application restarts.  The job is registered under a fixed ID with
``replace_existing=True`` so restarts never create duplicates.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import STATE_RUNNING
from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from linkvault.database import AsyncSessionLocal
from linkvault.models.link import Link

logger = logging.getLogger(__name__)

CLEANUP_JOB_ID = "expired_link_cleanup"
CLEANUP_INTERVAL_MINUTES = 15


# ---------------------------------------------------------------------------
# The job itself
# ---------------------------------------------------------------------------


async def expire_links(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    """Soft-delete every expired, not-yet-deleted link. Returns the count.

    Uses a single ``UPDATE ... WHERE expires_at < now AND deleted_at IS NULL``
    so already-deleted links keep their original ``deleted_at``.
    """
    factory = session_factory or AsyncSessionLocal
    now_utc = datetime.now(timezone.utc)
    # Datetimes are stored naive in UTC (see DECISIONS.md §4).
    now_naive = now_utc.replace(tzinfo=None)

    async with factory() as session:
        result: CursorResult[tuple[()]] = await session.execute(  # type: ignore[assignment]
            update(Link)
            .where(
                Link.expires_at.is_not(None),
                Link.expires_at < now_naive,
                Link.deleted_at.is_(None),
            )
            .values(deleted_at=now_naive)
            .execution_options(synchronize_session=False)
        )
        await session.commit()

    count = int(result.rowcount or 0)
    logger.info(
        json.dumps(
            {
                "event": "cleanup",
                "links_expired": count,
                "timestamp": now_utc.isoformat().replace("+00:00", "Z"),
            }
        )
    )
    return count


async def run_cleanup_job() -> None:
    """Entry point used by the scheduler (must be importable by reference)."""
    try:
        await expire_links()
    except Exception:
        logger.exception("Expired link cleanup job failed")


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------


def create_scheduler(jobstore_url: str) -> AsyncIOScheduler:
    """Build an (unstarted) scheduler backed by a persistent SQLite job store."""
    return AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=jobstore_url)},
        job_defaults={"coalesce": True, "max_instances": 1},
        timezone=timezone.utc,
    )


def schedule_cleanup_job(scheduler: AsyncIOScheduler) -> None:
    """Register (or replace) the single 15-minute cleanup job."""
    scheduler.add_job(
        run_cleanup_job,
        trigger="interval",
        minutes=CLEANUP_INTERVAL_MINUTES,
        id=CLEANUP_JOB_ID,
        name="Expired link cleanup",
        replace_existing=True,
        misfire_grace_time=CLEANUP_INTERVAL_MINUTES * 60,
    )


def scheduler_status(scheduler: AsyncIOScheduler | None) -> str:
    """Return ``"running"`` only if the scheduler is actively running.

    A missing, stopped or paused scheduler is reported as ``"stopped"``.
    """
    if scheduler is not None and scheduler.state == STATE_RUNNING:
        return "running"
    return "stopped"
