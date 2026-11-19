"""Expired link cleanup job — Item 5."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from linkvault.config import settings
from linkvault.models.link import Link

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core cleanup logic (async, works with any AsyncSession)
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    """Return a naive UTC datetime (matches how the models store timestamps)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def run_cleanup(db: AsyncSession) -> int:
    """Soft-delete links that are expired or have exhausted their click cap.

    Returns the number of links marked as deleted.
    """
    now = _utc_now()

    # Links that have passed their expiry time
    expired_stmt = (
        select(Link.id)
        .where(
            Link.deleted_at.is_(None),
            Link.expires_at.isnot(None),
            Link.expires_at < now,
        )
    )

    # Links that have hit or exceeded their max-click cap
    capped_stmt = (
        select(Link.id)
        .where(
            Link.deleted_at.is_(None),
            Link.max_clicks.isnot(None),
            Link.click_count >= Link.max_clicks,
        )
    )

    expired_ids_result = await db.execute(expired_stmt)
    capped_ids_result = await db.execute(capped_stmt)

    ids_to_delete: set[str] = {row[0] for row in expired_ids_result.fetchall()} | {
        row[0] for row in capped_ids_result.fetchall()
    }

    if ids_to_delete:
        await db.execute(
            update(Link)
            .where(Link.id.in_(ids_to_delete))
            .values(deleted_at=now)
        )
        await db.commit()

    count = len(ids_to_delete)
    logger.info(
        json.dumps(
            {
                "event": "cleanup",
                "links_expired": count,
                "timestamp": now.isoformat() + "Z",
            }
        )
    )
    return count


# ---------------------------------------------------------------------------
# Scheduled job entry-point (called by APScheduler in a thread)
# ---------------------------------------------------------------------------


def cleanup_job() -> None:
    """Entry-point for the APScheduler job.

    APScheduler runs jobs in a thread pool by default, so we spin up a
    fresh event loop for each invocation and use a dedicated engine/session
    so we don't share state with the web-server's connection pool.
    """
    engine = create_async_engine(settings.DATABASE_URL, future=True)
    factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )

    async def _run() -> None:
        async with factory() as session:
            await run_cleanup(session)
        await engine.dispose()

    asyncio.run(_run())
