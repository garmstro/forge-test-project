"""
Tests for the expired link cleanup job (Item 5).

Covers:
- A link past its expiry is soft-deleted; its short URL then returns 404 (not 410)
- Links with a future expiry, no expiry, or already deleted are untouched
- Click-capped links are not removed by this job
- A structured log entry with the correct count is written (including N = 0)
- A cleaned-up slug can be reused and click history stays with the old link
- A failing run is logged and does not raise
- The job is registered every 15 minutes in the persistent store, exactly once,
  across restarts
- /health reports the scheduler as "running" while the app is up
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from linkvault.config import settings
from linkvault.main import app
from linkvault.models.click import Click
from linkvault.models.link import Link
from linkvault.services import cleanup
from linkvault.services.cleanup import (
    CLEANUP_INTERVAL,
    CLEANUP_JOB_ID,
    CLEANUP_JOB_REF,
    cleanup_expired_links_job,
    create_scheduler,
    ensure_cleanup_job,
    run_cleanup,
    stop_scheduler,
)

REGISTER_URL = "/users/register"
LINKS_URL = "/links"
CLEANUP_LOGGER = "linkvault.services.cleanup"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)


async def register_and_get_key(client: AsyncClient, email: str) -> str:
    resp = await client.post(REGISTER_URL, json={"email": email, "password": "password123"})
    assert resp.status_code == 201, resp.text
    return resp.json()["api_key"]


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def create_link(
    client: AsyncClient,
    api_key: str,
    slug: str,
    expires_at: datetime | None = None,
    max_clicks: int | None = None,
) -> dict:
    payload: dict = {"url": f"https://example.com/{slug}", "slug": slug}
    if expires_at is not None:
        payload["expires_at"] = iso(expires_at)
    if max_clicks is not None:
        payload["max_clicks"] = max_clicks
    resp = await client.post(LINKS_URL, json=payload, headers=auth(api_key))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def get_deleted_at(
    factory: async_sessionmaker[AsyncSession], link_id: str
) -> datetime | None:
    async with factory() as session:
        result = await session.execute(select(Link.deleted_at).where(Link.id == link_id))
        return result.scalar_one()


def cleanup_events(caplog: pytest.LogCaptureFixture, event: str = "cleanup") -> list[dict]:
    events = []
    for record in caplog.records:
        if record.name != CLEANUP_LOGGER:
            continue
        try:
            payload = json.loads(record.getMessage())
        except ValueError:
            continue
        if payload.get("event") == event:
            events.append(payload)
    return events


# ---------------------------------------------------------------------------
# Cleanup behaviour
# ---------------------------------------------------------------------------


async def test_expired_link_is_soft_deleted(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    key = await register_and_get_key(client, "clean1@example.com")
    link = await create_link(client, key, "gone1", expires_at=utcnow() - timedelta(minutes=5))

    before = await client.get("/gone1", follow_redirects=False)
    assert before.status_code == 410

    now = utcnow()
    count = await run_cleanup(session_factory, now=now)

    assert count == 1
    assert await get_deleted_at(session_factory, link["id"]) == now

    after = await client.get("/gone1", follow_redirects=False)
    assert after.status_code == 404
    assert after.json()["error"] == "not_found"


async def test_future_and_permanent_links_untouched(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    key = await register_and_get_key(client, "clean2@example.com")
    future = await create_link(client, key, "future1", expires_at=utcnow() + timedelta(hours=1))
    forever = await create_link(client, key, "forever1")

    count = await run_cleanup(session_factory)

    assert count == 0
    assert await get_deleted_at(session_factory, future["id"]) is None
    assert await get_deleted_at(session_factory, forever["id"]) is None
    assert (await client.get("/future1", follow_redirects=False)).status_code == 302
    assert (await client.get("/forever1", follow_redirects=False)).status_code == 301


async def test_already_deleted_link_untouched(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    key = await register_and_get_key(client, "clean3@example.com")
    link = await create_link(client, key, "olddel1", expires_at=utcnow() - timedelta(days=1))
    resp = await client.delete(f"{LINKS_URL}/olddel1", headers=auth(key))
    assert resp.status_code == 204
    original_deleted_at = await get_deleted_at(session_factory, link["id"])
    assert original_deleted_at is not None

    count = await run_cleanup(session_factory, now=utcnow() + timedelta(hours=1))

    assert count == 0
    assert await get_deleted_at(session_factory, link["id"]) == original_deleted_at


async def test_click_capped_link_not_removed(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    key = await register_and_get_key(client, "clean4@example.com")
    link = await create_link(client, key, "capped1", max_clicks=1)
    assert (await client.get("/capped1", follow_redirects=False)).status_code == 302
    assert (await client.get("/capped1", follow_redirects=False)).status_code == 410

    count = await run_cleanup(session_factory)

    assert count == 0
    assert await get_deleted_at(session_factory, link["id"]) is None
    assert (await client.get("/capped1", follow_redirects=False)).status_code == 410


async def test_cleanup_logs_structured_entry(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=CLEANUP_LOGGER)
    key = await register_and_get_key(client, "clean5@example.com")
    past = utcnow() - timedelta(minutes=1)
    await create_link(client, key, "logexp1", expires_at=past)
    await create_link(client, key, "logexp2", expires_at=past)
    await create_link(client, key, "logkeep1", expires_at=utcnow() + timedelta(days=1))

    now = utcnow()
    await run_cleanup(session_factory, now=now)
    # Second run has nothing left to do but must still log.
    await run_cleanup(session_factory, now=now)

    events = cleanup_events(caplog)
    assert [e["links_expired"] for e in events] == [2, 0]
    assert set(events[0]) == {"event", "links_expired", "timestamp"}
    assert datetime.fromisoformat(events[0]["timestamp"]) == now.replace(tzinfo=timezone.utc)


async def test_cleaned_slug_reusable_and_clicks_kept_separate(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = await register_and_get_key(client, "clean6@example.com")
    expires = utcnow() + timedelta(minutes=10)
    old = await create_link(client, key, "reuse1", expires_at=expires)
    for _ in range(2):
        assert (await client.get("/reuse1", follow_redirects=False)).status_code == 302

    # Run the job as if it were after the expiry time.
    assert await run_cleanup(session_factory, now=expires + timedelta(seconds=1)) == 1

    new = await create_link(client, key, "reuse1")
    assert new["id"] != old["id"]
    assert new["click_count"] == 0
    assert (await client.get("/reuse1", follow_redirects=False)).status_code == 301

    stats = await client.get("/analytics/reuse1", headers=auth(key))
    assert stats.status_code == 200, stats.text
    assert stats.json()["total_clicks"] == 1

    async with session_factory() as session:
        old_clicks = await session.execute(
            select(func.count(Click.id)).where(Click.link_id == old["id"])
        )
        assert old_clicks.scalar_one() == 2


async def test_failed_run_is_logged_and_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=CLEANUP_LOGGER)

    async def boom() -> int:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cleanup, "run_cleanup", boom)

    await cleanup_expired_links_job()  # must not raise

    failures = cleanup_events(caplog, event="cleanup_failed")
    assert len(failures) == 1
    assert "database is locked" in failures[0]["error"]


# ---------------------------------------------------------------------------
# Scheduler registration & persistence
# ---------------------------------------------------------------------------


def scheduler_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'scheduler.db'}"


async def test_job_registered_every_15_minutes(tmp_path: Path) -> None:
    scheduler = create_scheduler(scheduler_url(tmp_path))
    scheduler.start()
    try:
        job = ensure_cleanup_job(scheduler)
        jobs = scheduler.get_jobs()
        assert [j.id for j in jobs] == [CLEANUP_JOB_ID]
        assert job.func_ref == CLEANUP_JOB_REF
        assert job.trigger.interval == CLEANUP_INTERVAL == timedelta(minutes=15)
        assert job.next_run_time is not None
    finally:
        await stop_scheduler(scheduler)
    assert not scheduler.running


async def test_restart_does_not_duplicate_job(tmp_path: Path) -> None:
    url = scheduler_url(tmp_path)

    first = create_scheduler(url)
    first.start()
    original_next_run = ensure_cleanup_job(first).next_run_time
    await stop_scheduler(first)

    second = create_scheduler(url)
    second.start()
    try:
        # Job was persisted and reloaded from disk.
        assert second.get_job(CLEANUP_JOB_ID) is not None
        job = ensure_cleanup_job(second)
        assert len(second.get_jobs()) == 1
        # Restart keeps the existing schedule rather than pushing it back.
        assert job.next_run_time == original_next_run
    finally:
        await stop_scheduler(second)


async def test_outdated_job_is_replaced_not_duplicated(tmp_path: Path) -> None:
    url = scheduler_url(tmp_path)

    first = create_scheduler(url)
    first.start()
    first.add_job(CLEANUP_JOB_REF, "interval", minutes=5, id=CLEANUP_JOB_ID)
    await stop_scheduler(first)

    second = create_scheduler(url)
    second.start()
    try:
        ensure_cleanup_job(second)
        jobs = second.get_jobs()
        assert len(jobs) == 1
        assert jobs[0].trigger.interval == timedelta(minutes=15)
    finally:
        await stop_scheduler(second)


async def test_app_lifespan_starts_scheduler_and_health_reports_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "SCHEDULER_DB_URL", scheduler_url(tmp_path))
    transport = ASGITransport(app=app)

    for _ in range(2):  # start → stop → start again (simulated restart)
        async with app.router.lifespan_context(app):
            scheduler = app.state.scheduler
            assert scheduler is not None and scheduler.running
            assert [j.id for j in scheduler.get_jobs()] == [CLEANUP_JOB_ID]

            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.get("/health")
            assert resp.status_code == 200
            assert resp.json()["scheduler"] == "running"

        assert app.state.scheduler is None
        assert not scheduler.running

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")
    assert resp.status_code == 200
    assert resp.json()["scheduler"] == "not_started"
