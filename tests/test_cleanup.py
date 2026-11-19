"""
Tests for the expired-link cleanup job (Step 5).

Covers:
- Links with a past expiry are soft-deleted
- Links with a future expiry / no expiry are left alone
- Already-deleted links keep their original deleted_at
- Only expired links are counted, and the structured log line reports N
- A log line is written even when nothing expired
- After cleanup, an expired slug returns 404 and can be reused
- The scheduler registers exactly one 15-minute job in a persistent store,
  and restarting does not create a duplicate
- The app lifespan starts and stops the scheduler
- Job failures are logged and do not propagate
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from linkvault.models.link import Link
from linkvault.models.user import User
from linkvault.services import cleanup
from linkvault.services.cleanup import (
    CLEANUP_JOB_ID,
    create_scheduler,
    expire_links,
    run_cleanup_job,
)

NOW = datetime(2025, 6, 1, 12, 0, 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)


async def _make_user(factory: async_sessionmaker[AsyncSession]) -> str:
    async with factory() as session:
        user = User(email="cleanup@example.com", password_hash="x", api_key_hash="y")
        session.add(user)
        await session.commit()
        return user.id


async def _make_link(
    factory: async_sessionmaker[AsyncSession],
    user_id: str,
    slug: str,
    expires_at: datetime | None = None,
    deleted_at: datetime | None = None,
) -> str:
    async with factory() as session:
        link = Link(
            user_id=user_id,
            slug=slug,
            destination_url="https://example.com/",
            expires_at=expires_at,
            deleted_at=deleted_at,
        )
        session.add(link)
        await session.commit()
        return link.id


async def _get_link(factory: async_sessionmaker[AsyncSession], link_id: str) -> Link:
    async with factory() as session:
        result = await session.execute(select(Link).where(Link.id == link_id))
        return result.scalar_one()


def _cleanup_records(caplog: pytest.LogCaptureFixture) -> list[dict]:
    out = []
    for rec in caplog.records:
        if rec.name != cleanup.__name__:
            continue
        try:
            payload = json.loads(rec.getMessage())
        except ValueError:
            continue
        if payload.get("event") == "cleanup":
            out.append(payload)
    return out


# ---------------------------------------------------------------------------
# expire_links
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_past_expiry_is_soft_deleted(session_factory) -> None:
    uid = await _make_user(session_factory)
    lid = await _make_link(session_factory, uid, "past", expires_at=NOW - timedelta(minutes=1))

    count = await expire_links(session_factory, now=NOW)

    assert count == 1
    link = await _get_link(session_factory, lid)
    assert link.deleted_at == NOW


@pytest.mark.asyncio
async def test_future_expiry_untouched(session_factory) -> None:
    uid = await _make_user(session_factory)
    lid = await _make_link(session_factory, uid, "future", expires_at=NOW + timedelta(days=1))

    assert await expire_links(session_factory, now=NOW) == 0
    assert (await _get_link(session_factory, lid)).deleted_at is None


@pytest.mark.asyncio
async def test_no_expiry_untouched(session_factory) -> None:
    uid = await _make_user(session_factory)
    lid = await _make_link(session_factory, uid, "forever")

    assert await expire_links(session_factory, now=NOW) == 0
    assert (await _get_link(session_factory, lid)).deleted_at is None


@pytest.mark.asyncio
async def test_already_deleted_keeps_original_deleted_at(session_factory) -> None:
    uid = await _make_user(session_factory)
    original = NOW - timedelta(days=3)
    lid = await _make_link(
        session_factory,
        uid,
        "gone",
        expires_at=NOW - timedelta(days=5),
        deleted_at=original,
    )

    assert await expire_links(session_factory, now=NOW) == 0
    assert (await _get_link(session_factory, lid)).deleted_at == original


@pytest.mark.asyncio
async def test_counts_only_expired_and_logs_structured_line(
    session_factory, caplog: pytest.LogCaptureFixture
) -> None:
    uid = await _make_user(session_factory)
    await _make_link(session_factory, uid, "exp-a", expires_at=NOW - timedelta(hours=1))
    await _make_link(session_factory, uid, "exp-b", expires_at=NOW - timedelta(days=10))
    await _make_link(session_factory, uid, "live", expires_at=NOW + timedelta(hours=1))
    await _make_link(session_factory, uid, "plain")
    await _make_link(
        session_factory, uid, "old", expires_at=NOW - timedelta(days=2), deleted_at=NOW - timedelta(days=1)
    )

    with caplog.at_level(logging.INFO, logger=cleanup.__name__):
        count = await expire_links(session_factory, now=NOW)

    assert count == 2
    records = _cleanup_records(caplog)
    assert len(records) == 1
    assert records[0]["links_expired"] == 2
    assert datetime.fromisoformat(records[0]["timestamp"]).replace(tzinfo=None) == NOW
    assert set(records[0]) == {"event", "links_expired", "timestamp"}


@pytest.mark.asyncio
async def test_logs_even_when_nothing_expired(
    session_factory, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=cleanup.__name__):
        assert await expire_links(session_factory) == 0

    records = _cleanup_records(caplog)
    assert len(records) == 1
    assert records[0]["links_expired"] == 0


@pytest.mark.asyncio
async def test_job_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(cleanup, "expire_links", boom)
    with caplog.at_level(logging.ERROR, logger=cleanup.__name__):
        await run_cleanup_job()  # must not raise

    failures = [json.loads(r.getMessage()) for r in caplog.records if r.name == cleanup.__name__]
    assert failures and failures[0]["event"] == "cleanup_failed"
    assert "db down" in failures[0]["error"]


# ---------------------------------------------------------------------------
# End-to-end: after cleanup the slug is 404 and reusable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_link_404_and_slug_reusable_after_cleanup(
    client: AsyncClient, session_factory
) -> None:
    resp = await client.post(
        "/users/register", json={"email": "e2e@example.com", "password": "password123"}
    )
    key = resp.json()["api_key"]
    headers = {"Authorization": f"Bearer {key}"}

    resp = await client.post(
        "/links",
        json={"url": "https://example.com/old", "slug": "reuse-me", "expires_at": "2020-01-01T00:00:00Z"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text

    # Before cleanup: expired but not deleted -> 410
    assert (await client.get("/reuse-me", follow_redirects=False)).status_code == 410

    assert await expire_links(session_factory) == 1

    # After cleanup: soft-deleted -> 404
    resp = await client.get("/reuse-me", follow_redirects=False)
    assert resp.status_code == 404
    assert resp.json() == {"error": "not_found"}

    # Slug is free again
    resp = await client.post(
        "/links", json={"url": "https://example.com/new", "slug": "reuse-me"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    resp = await client.get("/reuse-me", follow_redirects=False)
    assert resp.headers["location"] == "https://example.com/new"


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_registers_single_15_minute_job(tmp_path: Path) -> None:
    db_file = tmp_path / "scheduler.db"
    url = f"sqlite:///{db_file}"

    scheduler = create_scheduler(url)
    scheduler.start(paused=True)
    try:
        jobs = scheduler.get_jobs()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.id == CLEANUP_JOB_ID
        assert isinstance(job.trigger, IntervalTrigger)
        assert job.trigger.interval == timedelta(minutes=15)
        assert job.func is run_cleanup_job
    finally:
        scheduler.shutdown(wait=False)

    # The schedule is persisted to the SQLite file, not only kept in memory.
    assert db_file.exists()


@pytest.mark.asyncio
async def test_scheduler_restart_does_not_duplicate_job(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'scheduler.db'}"

    first = create_scheduler(url)
    first.start(paused=True)
    first.shutdown(wait=False)

    second = create_scheduler(url)
    second.start(paused=True)
    try:
        jobs = second.get_jobs()
        assert [j.id for j in jobs] == [CLEANUP_JOB_ID]
    finally:
        second.shutdown(wait=False)

    # A scheduler that never calls add_job still sees the persisted job.
    reader = AsyncIOScheduler(jobstores={"default": SQLAlchemyJobStore(url=url)})
    reader.start(paused=True)
    try:
        assert [j.id for j in reader.get_jobs()] == [CLEANUP_JOB_ID]
    finally:
        reader.shutdown(wait=False)


@pytest.mark.asyncio
async def test_app_lifespan_starts_and_stops_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from linkvault.config import settings
    from linkvault.main import app, lifespan

    monkeypatch.setattr(settings, "SCHEDULER_DB_URL", f"sqlite:///{tmp_path / 'scheduler.db'}")

    async with lifespan(app):
        scheduler = app.state.scheduler
        assert scheduler is not None and scheduler.running
        assert [j.id for j in scheduler.get_jobs()] == [CLEANUP_JOB_ID]

    assert not scheduler.running
