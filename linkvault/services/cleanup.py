"""Expired link cleanup job (AGENTS.md, Item 5).

Every 15 minutes, soft-delete links whose ``expires_at`` has passed.  The job
is scheduled with APScheduler using a SQLite-backed (SQLAlchemy) job store so
that it survives application restarts.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore  # type: ignore[import-untyped]
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from linkvault.config import settings
from linkvault.database import AsyncSessionLocal
from linkvault.models.link import Link

logger = logging.getLogger(__name__)

CLEANUP_JOB_ID = "expired_link_cleanup"
CLEANUP_INTERVAL_MINUTES = 15

# The scheduler owned by the running application (set by ``start_scheduler``).
_scheduler: AsyncIOScheduler | None = None


def _utc_now() -> datetime:
    """Naive UTC "now", matching how datetimes are stored in the database."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def expire_links(
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> int:
    """Soft-delete all expired, not-yet-deleted links and return how many."""
    now = _utc_now()
    async with session_factory() as session:
        result = await session.execute(
            update(Link)
            .where(Link.expires_at < now, Link.deleted_at.is_(None))
            .values(deleted_at=now)
        )
        await session.commit()
        expired = result.rowcount if isinstance(result, CursorResult) else 0

    logger.info(
        json.dumps(
            {
                "event": "cleanup",
                "links_expired": expired,
                "timestamp": now.replace(tzinfo=timezone.utc).isoformat(),
            }
        )
    )
    return expired


async def run_cleanup_job() -> None:
    """Scheduler entry point (referenced by import path in the job store)."""
    try:
        await expire_links()
    except Exception:  # pragma: no cover - keep the schedule alive
        logger.exception("Expired link cleanup failed")


def create_scheduler(jobstore_url: str | None = None) -> AsyncIOScheduler:
    """Build a scheduler with a persistent SQLite job store and the cleanup job."""
    url = jobstore_url or settings.SCHEDULER_DB_URL
    scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=url)},
        timezone="UTC",
    )
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


def start_scheduler(jobstore_url: str | None = None) -> AsyncIOScheduler:
    """Create and start the application scheduler (must be called inside a running loop)."""
    global _scheduler
    _scheduler = create_scheduler(jobstore_url)
    _scheduler.start()
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None


def scheduler_status() -> str:
    """``"running"`` if the application scheduler is active, else ``"stopped"``."""
    return "running" if _scheduler is not None and _scheduler.running else "stopped"
