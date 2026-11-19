"""Expired link cleanup job.

Every ``CLEANUP_INTERVAL_MINUTES`` (default 15) the scheduler started in
:mod:`linkvault.main` calls :func:`run_cleanup_job`, which soft-deletes every
link whose ``expires_at`` lies in the past and that has not been deleted yet.

The work is done in a single ``UPDATE`` statement so it is atomic and does not
load rows into memory.  Each run emits exactly one structured log line::

    {"event": "cleanup", "links_expired": N, "timestamp": "..."}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from linkvault.models.link import Link

logger = logging.getLogger(__name__)

#: Stable job id so restarts replace the persisted job instead of duplicating it.
CLEANUP_JOB_ID = "expired_link_cleanup"

#: Textual reference used when registering the job with a persistent job store.
CLEANUP_JOB_REF = "linkvault.services.cleanup:run_cleanup_job"


async def expire_links(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    now: datetime | None = None,
) -> int:
    """Soft-delete all expired, not-yet-deleted links. Returns the number affected.

    Datetimes are stored as naive UTC (see DECISIONS.md §4), so the comparison
    uses a naive UTC "now".  Links that are already soft-deleted keep their
    original ``deleted_at``.
    """
    if session_factory is None:
        from linkvault.database import AsyncSessionLocal

        session_factory = AsyncSessionLocal

    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)

    stmt = (
        update(Link)
        .where(
            Link.expires_at.is_not(None),
            Link.expires_at < now,
            Link.deleted_at.is_(None),
        )
        .values(deleted_at=now)
        .execution_options(synchronize_session=False)
    )

    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(stmt)
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def run_cleanup_job(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    """Scheduler entry point: run :func:`expire_links` and log the outcome.

    Failures are logged and swallowed so a single bad run never stops the
    scheduler from executing later runs.  Returns the number of links
    expired (0 on failure).
    """
    started = datetime.now(timezone.utc)
    try:
        expired = await expire_links(session_factory, now=started)
    except Exception:
        logger.exception(
            json.dumps(
                {
                    "event": "cleanup_failed",
                    "timestamp": started.isoformat(),
                }
            )
        )
        return 0

    logger.info(
        json.dumps(
            {
                "event": "cleanup",
                "links_expired": expired,
                "timestamp": started.isoformat(),
            }
        )
    )
    return expired
