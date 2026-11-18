"""
Tests for the expired-link cleanup job and its background scheduler (Item 5).

Covers:
- Expired links get deleted_at set by the job
- Future-expiry, no-expiry and already-deleted links are left alone
  (already-deleted links keep their original deleted_at)
- The structured log line reports the correct count, including 0
- A failing run is logged and does not raise
- After cleanup, an expired link's slug can be reused
- App start-up registers exactly one 15-minute job; restart does not duplicate it
- The job is persisted in the scheduler database file
- Shutdown stops the scheduler cleanly
- The scheduled job actually invokes the cleanup
- Normal test clients do not start the scheduler
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from apscheduler.triggers.interval import IntervalTrigger
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from linkvault.config import settings
from linkvault.main import app
from linkvault.models.link import Link
from linkvault.models.user import User
from linkvault.services import cleanup
from linkvault.services.cleanup import (
    CLEANUP_JOB_ID,
    expire_links,
    get_scheduler,
    is_scheduler_running,
    run_cleanup_job,
    shutdown_scheduler,
)

CLEANUP_LOGGER = "linkvault.services.cleanup"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture()
def jobstore_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point the scheduler at a throwaway SQLite file for this test."""
    url = f"sqlite:///{tmp_path / 'scheduler.db'}"
    monkeypatch.setattr(settings, "SCHEDULER_DB_URL", url)
    return url


@pytest_asyncio.fixture(autouse=True)
async def _stop_scheduler_after_test() -> AsyncIterator[None]:
    """Never leak a running scheduler between tests."""
    yield
    await shutdown_scheduler()


async def make_user(factory: async_sessionmaker[AsyncSession]) -> str:
    async with factory() as session:
        user = User(email="cleanup@example.com", password_hash="x", api_key_hash="y")
        session.add(user)
        await session.commit()
        return user.id


async def make_link(
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


async def get_link(factory: async_sessionmaker[AsyncSession], link_id: str) -> Link:
    async with factory() as session:
        result = await session.execute(select(Link).where(Link.id == link_id))
        return result.scalar_one()


def cleanup_events(caplog: pytest.LogCaptureFixture) -> list[dict]:
    events = []
    for record in caplog.records:
        if record.name != CLEANUP_LOGGER:
            continue
        try:
            payload = json.loads(record.getMessage())
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("event", "").startswith("cleanup"):
            events.append(payload)
    return events


def stored_job_ids(url: str) -> list[str]:
    path = url.removeprefix("sqlite:///")
    with sqlite3.connect(path) as conn:
        return [row[0] for row in conn.execute("SELECT id FROM apscheduler_jobs")]


# ---------------------------------------------------------------------------
# The cleanup job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_link_gets_deleted_at(session_factory) -> None:
    user_id = await make_user(session_factory)
    link_id = await make_link(
        session_factory, user_id, "gone1", expires_at=utcnow() - timedelta(minutes=1)
    )

    count = await run_cleanup_job(session_factory)

    assert count == 1
    link = await get_link(session_factory, link_id)
    assert link.deleted_at is not None
    assert abs(link.deleted_at - utcnow()) < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_non_expired_and_deleted_links_untouched(session_factory) -> None:
    user_id = await make_user(session_factory)
    original_deleted_at = datetime(2020, 1, 1, 12, 0, 0)

    future_id = await make_link(
        session_factory, user_id, "future", expires_at=utcnow() + timedelta(days=1)
    )
    no_expiry_id = await make_link(session_factory, user_id, "forever")
    deleted_id = await make_link(
        session_factory,
        user_id,
        "olddel",
        expires_at=utcnow() - timedelta(days=10),
        deleted_at=original_deleted_at,
    )
    expired_id = await make_link(
        session_factory, user_id, "expired", expires_at=utcnow() - timedelta(seconds=5)
    )

    count = await run_cleanup_job(session_factory)

    assert count == 1
    assert (await get_link(session_factory, future_id)).deleted_at is None
    assert (await get_link(session_factory, no_expiry_id)).deleted_at is None
    assert (await get_link(session_factory, deleted_id)).deleted_at == original_deleted_at
    assert (await get_link(session_factory, expired_id)).deleted_at is not None


@pytest.mark.asyncio
async def test_expire_links_uses_given_now(session_factory) -> None:
    """Links expiring exactly at or after *now* are not yet expired."""
    user_id = await make_user(session_factory)
    now = datetime(2030, 6, 1, 12, 0, 0)
    before_id = await make_link(
        session_factory, user_id, "before", expires_at=now - timedelta(seconds=1)
    )
    exact_id = await make_link(session_factory, user_id, "exact", expires_at=now)

    async with session_factory() as session:
        count = await expire_links(session, now)
        await session.commit()

    assert count == 1
    assert (await get_link(session_factory, before_id)).deleted_at == now
    assert (await get_link(session_factory, exact_id)).deleted_at is None


@pytest.mark.asyncio
async def test_cleanup_logs_structured_count(session_factory, caplog) -> None:
    user_id = await make_user(session_factory)
    past = utcnow() - timedelta(hours=1)
    for slug in ("log1", "log2", "log3"):
        await make_link(session_factory, user_id, slug, expires_at=past)

    with caplog.at_level(logging.INFO, logger=CLEANUP_LOGGER):
        await run_cleanup_job(session_factory)

    events = cleanup_events(caplog)
    assert len(events) == 1
    event = events[0]
    assert set(event) == {"event", "links_expired", "timestamp"}
    assert event["event"] == "cleanup"
    assert event["links_expired"] == 3
    parsed = datetime.fromisoformat(event["timestamp"])
    assert parsed.utcoffset() == timedelta(0)


@pytest.mark.asyncio
async def test_cleanup_logs_zero_when_nothing_expired(session_factory, caplog) -> None:
    with caplog.at_level(logging.INFO, logger=CLEANUP_LOGGER):
        count = await run_cleanup_job(session_factory)

    assert count == 0
    events = cleanup_events(caplog)
    assert [e["links_expired"] for e in events] == [0]


@pytest.mark.asyncio
async def test_cleanup_failure_is_logged_not_raised(caplog) -> None:
    def broken_factory():
        raise RuntimeError("database unavailable")

    with caplog.at_level(logging.INFO, logger=CLEANUP_LOGGER):
        result = await run_cleanup_job(broken_factory)  # type: ignore[arg-type]

    assert result is None
    events = cleanup_events(caplog)
    assert len(events) == 1
    assert events[0]["event"] == "cleanup_failed"
    assert "database unavailable" in events[0]["error"]
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
async def test_expired_slug_reusable_after_cleanup(client: AsyncClient, db_engine) -> None:
    resp = await client.post(
        "/users/register", json={"email": "reuse@example.com", "password": "password123"}
    )
    key = resp.json()["api_key"]
    headers = {"Authorization": f"Bearer {key}"}
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    resp = await client.post(
        "/links", json={"url": "https://old.example.com/", "slug": "reuse-me", "expires_at": past},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text

    # Before cleanup, the expired link still holds the slug.
    resp = await client.post(
        "/links", json={"url": "https://new.example.com/", "slug": "reuse-me"}, headers=headers
    )
    assert resp.status_code == 409

    factory = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)
    assert await run_cleanup_job(factory) == 1

    # The old link now behaves as deleted: 404 rather than 410.
    resp = await client.get("/reuse-me", follow_redirects=False)
    assert resp.status_code == 404

    resp = await client.post(
        "/links", json={"url": "https://new.example.com/", "slug": "reuse-me"}, headers=headers
    )
    assert resp.status_code == 201, resp.text

    resp = await client.get("/reuse-me", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"] == "https://new.example.com/"


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_registers_single_15_minute_job(jobstore_url) -> None:
    async with app.router.lifespan_context(app):
        assert is_scheduler_running()
        scheduler = get_scheduler()
        assert scheduler is not None
        jobs = scheduler.get_jobs()
        assert [job.id for job in jobs] == [CLEANUP_JOB_ID]
        job = jobs[0]
        assert isinstance(job.trigger, IntervalTrigger)
        assert job.trigger.interval == timedelta(minutes=15)
        assert job.func_ref == "linkvault.services.cleanup:run_cleanup_job"
        assert job.next_run_time is not None

    assert not is_scheduler_running()


@pytest.mark.asyncio
async def test_job_is_persisted_in_scheduler_db(jobstore_url) -> None:
    async with app.router.lifespan_context(app):
        assert stored_job_ids(jobstore_url) == [CLEANUP_JOB_ID]

    # Still on disk after shutdown — not just held in memory.
    assert stored_job_ids(jobstore_url) == [CLEANUP_JOB_ID]


@pytest.mark.asyncio
async def test_restart_does_not_duplicate_job(jobstore_url) -> None:
    async with app.router.lifespan_context(app):
        first_scheduler = get_scheduler()
        assert first_scheduler is not None
        first_next_run = first_scheduler.get_job(CLEANUP_JOB_ID).next_run_time

    async with app.router.lifespan_context(app):
        scheduler = get_scheduler()
        assert scheduler is not None
        assert scheduler is not first_scheduler
        jobs = scheduler.get_jobs()
        assert [job.id for job in jobs] == [CLEANUP_JOB_ID]
        # The persisted schedule is kept rather than reset on every restart.
        assert jobs[0].next_run_time == first_next_run

    assert stored_job_ids(jobstore_url) == [CLEANUP_JOB_ID]


@pytest.mark.asyncio
async def test_shutdown_is_clean(jobstore_url, caplog) -> None:
    with caplog.at_level(logging.INFO):
        async with app.router.lifespan_context(app):
            scheduler = get_scheduler()
            assert scheduler is not None and scheduler.running

    assert not scheduler.running
    assert get_scheduler() is None
    assert not is_scheduler_running()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    # Shutting down again is a harmless no-op.
    await shutdown_scheduler()


@pytest.mark.asyncio
async def test_scheduled_job_runs_cleanup(jobstore_url, session_factory, monkeypatch, caplog) -> None:
    """The persisted job reference resolves to the cleanup and runs on the loop."""
    user_id = await make_user(session_factory)
    link_id = await make_link(
        session_factory, user_id, "sched", expires_at=utcnow() - timedelta(minutes=1)
    )
    monkeypatch.setattr(cleanup, "AsyncSessionLocal", session_factory)

    with caplog.at_level(logging.INFO, logger=CLEANUP_LOGGER):
        async with app.router.lifespan_context(app):
            scheduler = get_scheduler()
            assert scheduler is not None
            # Make the job due now instead of waiting 15 minutes.
            scheduler.get_job(CLEANUP_JOB_ID).modify(
                next_run_time=datetime.now(timezone.utc)
            )
            scheduler.wakeup()
            for _ in range(100):
                if cleanup_events(caplog):
                    break
                await asyncio.sleep(0.05)

    assert [e["links_expired"] for e in cleanup_events(caplog)] == [1]
    assert (await get_link(session_factory, link_id)).deleted_at is not None


@pytest.mark.asyncio
async def test_test_client_does_not_start_scheduler(client: AsyncClient) -> None:
    """httpx's ASGITransport does not run lifespan events, so no scheduler."""
    resp = await client.post(
        "/users/register", json={"email": "nosched@example.com", "password": "password123"}
    )
    assert resp.status_code == 201
    assert not is_scheduler_running()
    assert get_scheduler() is None
