"""Tests for the expired-link cleanup job and the scheduler (AGENTS.md, Item 5)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from linkvault.models.link import Link
from linkvault.models.user import User
from linkvault.services.cleanup import (
    CLEANUP_JOB_ID,
    create_scheduler,
    expire_links,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def _seed(factory: async_sessionmaker[AsyncSession]) -> None:
    now = _now()
    async with factory() as session:
        session.add(User(id="u1", email="u1@example.com", password_hash="x", api_key_hash="k"))
        await session.flush()
        session.add_all(
            [
                Link(id="expired", user_id="u1", slug="expired", destination_url="https://a.example",
                     expires_at=now - timedelta(hours=1)),
                Link(id="future", user_id="u1", slug="future", destination_url="https://b.example",
                     expires_at=now + timedelta(hours=1)),
                Link(id="never", user_id="u1", slug="never", destination_url="https://c.example"),
                Link(id="already", user_id="u1", slug="already", destination_url="https://d.example",
                     expires_at=now - timedelta(days=2), deleted_at=now - timedelta(days=1)),
            ]
        )
        await session.commit()


async def test_expire_links_soft_deletes_only_expired_active_links(
    db_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    factory = _factory(db_engine)
    await _seed(factory)

    with caplog.at_level(logging.INFO, logger="linkvault.services.cleanup"):
        count = await expire_links(factory)

    assert count == 1
    async with factory() as session:
        rows = {
            link.id: link
            for link in (await session.execute(select(Link))).scalars().all()
        }
    assert rows["expired"].deleted_at is not None
    assert rows["future"].deleted_at is None
    assert rows["never"].deleted_at is None
    # Previously deleted link keeps its original deletion timestamp
    assert rows["already"].deleted_at is not None
    assert rows["already"].deleted_at < _now() - timedelta(hours=12)

    payloads = [json.loads(r.getMessage()) for r in caplog.records if r.name.endswith("cleanup")]
    assert len(payloads) == 1
    assert payloads[0]["event"] == "cleanup"
    assert payloads[0]["links_expired"] == 1
    assert "timestamp" in payloads[0]


async def test_expire_links_is_idempotent(db_engine: AsyncEngine) -> None:
    factory = _factory(db_engine)
    await _seed(factory)
    assert await expire_links(factory) == 1
    assert await expire_links(factory) == 0


async def test_scheduler_registers_15_minute_job(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'sched.db'}"
    scheduler = create_scheduler(url)
    scheduler.start(paused=True)
    try:
        job = scheduler.get_job(CLEANUP_JOB_ID)
        assert job is not None
        assert job.trigger.interval == timedelta(minutes=15)
    finally:
        scheduler.shutdown(wait=False)


async def test_job_survives_scheduler_restart(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'sched.db'}"
    first = create_scheduler(url)
    first.start(paused=True)
    first.shutdown(wait=False)
    assert (tmp_path / "sched.db").exists()

    # A fresh scheduler pointed at the same store finds the persisted job
    # without registering it again.
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore  # type: ignore[import-untyped]
    from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]

    second = AsyncIOScheduler(jobstores={"default": SQLAlchemyJobStore(url=url)})
    second.start(paused=True)
    try:
        assert second.get_job(CLEANUP_JOB_ID) is not None
    finally:
        second.shutdown(wait=False)
