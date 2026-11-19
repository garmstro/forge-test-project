"""Expired link cleanup job.

Every 15 minutes, links whose ``expires_at`` lies in the past and which have
not yet been soft-deleted are marked as deleted (``deleted_at = now``).  The
work is a single ``UPDATE`` statement so it is atomic and cheap regardless of
how many links are affected.

The job is scheduled with APScheduler using a SQLite-backed persistent job
store (``settings.SCHEDULER_DB_URL``), so the schedule survives application
restarts.  The job is registered under a fixed id with ``replace_existing``
so restarting the app never creates a duplicate.

All timestamps are naive UTC, matching how ``expires_at`` is stored (see
DECISIONS.md §4).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, cast

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from linkvault.config import settings
from linkvault.models.link import Link

logger = logging.getLogger(__name__)

CLEANUP_JOB_ID = "expired_link_cleanup"
CLEANUP_INTERVAL_MINUTES = 15
# Text reference so the persistent job store can re-import the job after a restart.
CLEANUP_JOB_REF = "linkvault.services.cleanup:run_cleanup_job"


def _utc_now() -> datetime:
    """Current time as a naive UTC datetime (the format stored in the DB)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def expire_links(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    now: datetime | None = None,
) -> int:
    """Soft-delete every expired, not-yet-deleted link and log the result.

    Returns the number of links that were marked as deleted.  Exactly one
    structured log line is written per call, even when nothing expired.
    """
    if session_factory is None:
        from linkvault.database import AsyncSessionLocal

        session_factory = AsyncSessionLocal

    if now is None:
        now = _utc_now()

    async with session_factory() as session:
        async with session.begin():
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(Link)
                    .where(
                        Link.expires_at.is_not(None),
                        Link.expires_at < now,
                        Link.deleted_at.is_(None),
                    )
                    .values(deleted_at=now)
                    .execution_options(synchronize_session=False)
                ),
            )
            links_expired = int(result.rowcount or 0)

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
    """Scheduler entry point: run :func:`expire_links`, never raising.

    Failures are logged so the scheduler keeps running and the next run
    happens as normal.
    """
    try:
        await expire_links()
    except Exception as exc:  # job must never crash the scheduler
        logger.exception(
            json.dumps(
                {
                    "event": "cleanup_failed",
                    "error": str(exc),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
        )


def create_scheduler(db_url: str | None = None) -> AsyncIOScheduler:
    """Build an AsyncIOScheduler with a persistent SQLite job store.

    The cleanup job is registered (or replaced, if it already exists in the
    store) to run every 15 minutes.  The scheduler is returned un-started;
    call ``.start()`` from within a running event loop.
    """
    scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=db_url or settings.SCHEDULER_DB_URL)},
        timezone=timezone.utc,
    )
    scheduler.add_job(
        CLEANUP_JOB_REF,
        trigger="interval",
        minutes=CLEANUP_INTERVAL_MINUTES,
        id=CLEANUP_JOB_ID,
        name="Expired link cleanup",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=CLEANUP_INTERVAL_MINUTES * 60,
    )
    return scheduler
