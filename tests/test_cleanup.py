"""Tests for the expired link cleanup job."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from linkvault.models.link import Link
from linkvault.models.user import User
from linkvault.services.cleanup import (
    CLEANUP_INTERVAL_MINUTES,
    CLEANUP_JOB_ID,
    create_scheduler,
    expire_stale_links,
)

NOW = datetime(2025, 6, 1, 12, 0, 0)


@pytest.fixture()
def factory(db_engine):
    return async_sessionmaker(
        bind=db_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


async def _seed(factory) -> dict[str, str]:
    """Create a user and links; returns slug -> id."""
    async with factory() as session:
        user = User(email="u@example.com", password_hash="x", api_key_hash="k")
        session.add(user)
        await session.flush()
        specs = {
            "expired": dict(expires_at=NOW - timedelta(minutes=1)),
            "future": dict(expires_at=NOW + timedelta(days=1)),
            "never": dict(expires_at=None),
            "already": dict(
                expires_at=NOW - timedelta(days=2), deleted_at=NOW - timedelta(days=1)
            ),
        }
        ids: dict[str, str] = {}
        for slug, extra in specs.items():
            link = Link(user_id=user.id, slug=slug, destination_url="https://example.com", **extra)
            session.add(link)
            await session.flush()
            ids[slug] = link.id
        await session.commit()
        return ids


async def test_expires_only_stale_active_links(factory) -> None:
    ids = await _seed(factory)
    count = await expire_stale_links(factory, now=NOW)
    assert count == 1
    async with factory() as session:
        rows = {
            link.slug: link
            for link in (await session.execute(select(Link))).scalars().all()
        }
    assert rows["expired"].deleted_at == NOW
    assert rows["future"].deleted_at is None
    assert rows["never"].deleted_at is None
    # Previously soft-deleted links keep their original deleted_at.
    assert rows["already"].deleted_at == NOW - timedelta(days=1)
    assert ids["expired"] == rows["expired"].id


async def test_second_run_is_noop(factory) -> None:
    await _seed(factory)
    assert await expire_stale_links(factory, now=NOW) == 1
    assert await expire_stale_links(factory, now=NOW) == 0


async def test_logs_structured_json(factory, caplog) -> None:
    await _seed(factory)
    with caplog.at_level(logging.INFO, logger="linkvault.services.cleanup"):
        await expire_stale_links(factory, now=NOW)
    payloads = [json.loads(r.getMessage()) for r in caplog.records if r.name.endswith("cleanup")]
    assert payloads == [
        {"event": "cleanup", "links_expired": 1, "timestamp": "2025-06-01T12:00:00Z"}
    ]


async def test_job_persists_across_scheduler_restart(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'scheduler.db'}"
    first = create_scheduler(url)
    first.start()
    first.shutdown(wait=False)

    # A "restarted" scheduler sees the persisted job even before adding it.
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    second = AsyncIOScheduler(jobstores={"default": SQLAlchemyJobStore(url=url)})
    second.start(paused=True)
    try:
        job = second.get_job(CLEANUP_JOB_ID)
        assert job is not None
        assert job.trigger.interval == timedelta(minutes=CLEANUP_INTERVAL_MINUTES)
    finally:
        second.shutdown(wait=False)

    # Re-creating the scheduler must not duplicate the job.
    third = create_scheduler(url)
    third.start(paused=True)
    try:
        assert len(third.get_jobs()) == 1
    finally:
        third.shutdown(wait=False)
