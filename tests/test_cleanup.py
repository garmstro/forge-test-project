"""
Tests for the expired link cleanup job (Item 5).

Covers:
- Expired links are soft-deleted and drop out of the owner's list and summary
- An expired-and-cleaned-up slug can be claimed by a new link
- Links with no expiry, a future expiry, or an earlier deletion are untouched
- One structured log line per run, with the correct count (including 0)
- The scheduler entry point uses its own session
- The scheduler persists the 15-minute job across restarts, exactly once
- /health reports the scheduler's real state through the app lifespan

The real scheduler is never started implicitly (see conftest.py); the
scheduler tests start one explicitly against a temporary job store.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from linkvault.config import settings
from linkvault.main import app
from linkvault.models.link import Link
from linkvault.services import cleanup
from linkvault.services.cleanup import (
    CLEANUP_INTERVAL_MINUTES,
    CLEANUP_JOB_ID,
    cleanup_expired_links,
    run_cleanup_job,
    scheduler_status,
    shutdown_scheduler,
    start_scheduler,
)

REGISTER_URL = "/users/register"
LINKS_URL = "/links"
CLEANUP_LOGGER = "linkvault.services.cleanup"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def register_and_get_key(
    client: AsyncClient, email: str, password: str = "password123"
) -> str:
    resp = await client.post(REGISTER_URL, json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()["api_key"]


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


async def create_link(
    client: AsyncClient, api_key: str, slug: str, expires_at: datetime | None = None
) -> dict:
    payload: dict = {"url": f"https://example.com/{slug}", "slug": slug}
    if expires_at is not None:
        payload["expires_at"] = _iso(expires_at)
    resp = await client.post(LINKS_URL, json=payload, headers=auth(api_key))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def get_link_row(db_session: AsyncSession, link_id: str) -> Link:
    db_session.expire_all()
    result = await db_session.execute(select(Link).where(Link.id == link_id))
    return result.scalar_one()


def cleanup_log_payloads(caplog: pytest.LogCaptureFixture) -> list[dict]:
    payloads = []
    for record in caplog.records:
        if record.name != CLEANUP_LOGGER:
            continue
        try:
            data = json.loads(record.getMessage())
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("event") == "cleanup":
            payloads.append(data)
    return payloads


@pytest.fixture(autouse=True)
def reset_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with no scheduler registered at module level."""
    monkeypatch.setattr(cleanup, "_scheduler", None)


@pytest_asyncio.fixture()
async def scheduler_db_url(tmp_path: Path) -> AsyncIterator[str]:
    """A throwaway SQLite job store; any scheduler left running is stopped."""
    yield f"sqlite:///{tmp_path / 'scheduler.db'}"
    await shutdown_scheduler()


# ---------------------------------------------------------------------------
# Cleanup logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_link_is_marked_deleted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    key = await register_and_get_key(client, "cleanup_expired@example.com")
    link = await create_link(client, key, "gone-soon", expires_at=_utc_now() - timedelta(minutes=5))

    now = _utc_now()
    count = await cleanup_expired_links(db_session, now=now)

    assert count == 1
    row = await get_link_row(db_session, link["id"])
    assert row.deleted_at is not None
    assert abs(row.deleted_at - now) < timedelta(seconds=1)


@pytest.mark.asyncio
async def test_expired_link_leaves_list_and_summary(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    key = await register_and_get_key(client, "cleanup_counts@example.com")
    await create_link(client, key, "keep-me")
    await create_link(client, key, "expire-me", expires_at=_utc_now() - timedelta(hours=1))

    before = (await client.get("/analytics/summary", headers=auth(key))).json()
    assert before["total_links"] == 2

    assert await cleanup_expired_links(db_session) == 1

    listing = (await client.get(LINKS_URL, headers=auth(key))).json()
    assert listing["total"] == 1
    assert [item["slug"] for item in listing["items"]] == ["keep-me"]

    summary = (await client.get("/analytics/summary", headers=auth(key))).json()
    assert summary["total_links"] == 1
    assert summary["active_links"] == 1

    assert (await client.get(f"{LINKS_URL}/expire-me", headers=auth(key))).status_code == 404


@pytest.mark.asyncio
async def test_expired_slug_can_be_reclaimed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    key_a = await register_and_get_key(client, "cleanup_old_owner@example.com")
    key_b = await register_and_get_key(client, "cleanup_new_owner@example.com")
    await create_link(client, key_a, "reclaim-me", expires_at=_utc_now() - timedelta(days=1))

    # While the expired link is still un-cleaned, the slug is taken.
    taken = await client.post(
        LINKS_URL, json={"url": "https://new.example.com/", "slug": "reclaim-me"}, headers=auth(key_b)
    )
    assert taken.status_code == 409

    await cleanup_expired_links(db_session)

    resp = await client.post(
        LINKS_URL, json={"url": "https://new.example.com/", "slug": "reclaim-me"}, headers=auth(key_b)
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["destination_url"] == "https://new.example.com/"


@pytest.mark.asyncio
async def test_non_expired_and_already_deleted_links_untouched(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    key = await register_and_get_key(client, "cleanup_untouched@example.com")
    no_expiry = await create_link(client, key, "no-expiry")
    future = await create_link(client, key, "future", expires_at=_utc_now() + timedelta(days=7))
    already = await create_link(
        client, key, "already-deleted", expires_at=_utc_now() - timedelta(days=3)
    )

    # Soft-delete one expired link earlier, with a known deletion time.
    original_deleted_at = _utc_now() - timedelta(days=2)
    row = await get_link_row(db_session, already["id"])
    row.deleted_at = original_deleted_at
    await db_session.commit()

    assert await cleanup_expired_links(db_session) == 0

    assert (await get_link_row(db_session, no_expiry["id"])).deleted_at is None
    assert (await get_link_row(db_session, future["id"])).deleted_at is None
    assert (await get_link_row(db_session, already["id"])).deleted_at == original_deleted_at


@pytest.mark.asyncio
async def test_cleanup_is_idempotent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    key = await register_and_get_key(client, "cleanup_twice@example.com")
    link = await create_link(client, key, "twice", expires_at=_utc_now() - timedelta(minutes=1))

    first_run = _utc_now()
    assert await cleanup_expired_links(db_session, now=first_run) == 1
    assert await cleanup_expired_links(db_session, now=first_run + timedelta(minutes=15)) == 0
    assert (await get_link_row(db_session, link["id"])).deleted_at == first_run


@pytest.mark.asyncio
async def test_cleanup_logs_structured_line_with_count(
    client: AsyncClient, db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    key = await register_and_get_key(client, "cleanup_log@example.com")
    past = _utc_now() - timedelta(minutes=30)
    for i in range(3):
        await create_link(client, key, f"log-{i}", expires_at=past)
    await create_link(client, key, "log-live")

    caplog.set_level(logging.INFO, logger=CLEANUP_LOGGER)
    now = _utc_now()
    await cleanup_expired_links(db_session, now=now)

    payloads = cleanup_log_payloads(caplog)
    assert len(payloads) == 1
    payload = payloads[0]
    assert set(payload) == {"event", "links_expired", "timestamp"}
    assert payload["event"] == "cleanup"
    assert payload["links_expired"] == 3
    logged_at = datetime.fromisoformat(payload["timestamp"])
    assert logged_at.utcoffset() == timedelta(0)
    assert logged_at.replace(tzinfo=None) == now


@pytest.mark.asyncio
async def test_cleanup_logs_when_nothing_expired(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=CLEANUP_LOGGER)

    assert await cleanup_expired_links(db_session) == 0

    payloads = cleanup_log_payloads(caplog)
    assert len(payloads) == 1
    assert payloads[0]["links_expired"] == 0


@pytest.mark.asyncio
async def test_run_cleanup_job_uses_its_own_session(
    client: AsyncClient,
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The scheduler entry point opens a session from AsyncSessionLocal and commits."""
    factory = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(cleanup, "AsyncSessionLocal", factory)

    key = await register_and_get_key(client, "cleanup_job@example.com")
    link = await create_link(client, key, "job-run", expires_at=_utc_now() - timedelta(minutes=1))

    caplog.set_level(logging.INFO, logger=CLEANUP_LOGGER)
    await run_cleanup_job()

    assert (await get_link_row(db_session, link["id"])).deleted_at is not None
    assert [p["links_expired"] for p in cleanup_log_payloads(caplog)] == [1]


# ---------------------------------------------------------------------------
# Scheduler lifecycle and persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_status_before_start() -> None:
    assert scheduler_status() == "not_started"


@pytest.mark.asyncio
async def test_scheduler_registers_job_once_and_survives_restart(
    scheduler_db_url: str,
) -> None:
    scheduler = start_scheduler(scheduler_db_url)
    assert scheduler_status() == "running"

    jobs = scheduler.get_jobs()
    assert [job.id for job in jobs] == [CLEANUP_JOB_ID]
    assert jobs[0].trigger.interval == timedelta(minutes=CLEANUP_INTERVAL_MINUTES)
    assert jobs[0].func is run_cleanup_job
    first_next_run = jobs[0].next_run_time

    await shutdown_scheduler()
    assert scheduler_status() == "stopped"

    # "Restart": a brand-new scheduler on the same persistent job store.
    restarted = start_scheduler(scheduler_db_url)
    assert restarted is not scheduler
    assert scheduler_status() == "running"

    jobs = restarted.get_jobs()
    assert [job.id for job in jobs] == [CLEANUP_JOB_ID]
    # The persisted schedule is kept rather than reset by the restart.
    assert jobs[0].next_run_time == first_next_run


@pytest.mark.asyncio
async def test_start_scheduler_is_idempotent_while_running(scheduler_db_url: str) -> None:
    first = start_scheduler(scheduler_db_url)
    second = start_scheduler(scheduler_db_url)
    assert first is second
    assert len(first.get_jobs()) == 1


@pytest.mark.asyncio
async def test_scheduler_fixes_stale_interval(scheduler_db_url: str) -> None:
    """A persisted job with a different interval is rescheduled, not duplicated."""
    scheduler = start_scheduler(scheduler_db_url)
    scheduler.reschedule_job(CLEANUP_JOB_ID, trigger="interval", minutes=60)
    await shutdown_scheduler()

    restarted = start_scheduler(scheduler_db_url)
    jobs = restarted.get_jobs()
    assert len(jobs) == 1
    assert jobs[0].trigger.interval == timedelta(minutes=CLEANUP_INTERVAL_MINUTES)


@pytest.mark.asyncio
async def test_shutdown_without_start_is_noop() -> None:
    await shutdown_scheduler()
    assert scheduler_status() == "not_started"


# ---------------------------------------------------------------------------
# /health and the app lifespan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_reports_not_started_when_scheduler_disabled(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["scheduler"] == "not_started"


@pytest.mark.asyncio
async def test_lifespan_disabled_does_not_start_scheduler() -> None:
    assert settings.SCHEDULER_ENABLED is False  # set by conftest.py
    async with app.router.lifespan_context(app):
        assert scheduler_status() == "not_started"


@pytest.mark.asyncio
async def test_health_reports_running_and_job_survives_app_restart(
    client: AsyncClient, scheduler_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", True)
    monkeypatch.setattr(settings, "SCHEDULER_DB_URL", scheduler_db_url)

    async with app.router.lifespan_context(app):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["scheduler"] == "running"

    resp = await client.get("/health")
    assert resp.json()["scheduler"] == "stopped"

    # Start the app again: still running, and the job is registered exactly once.
    async with app.router.lifespan_context(app):
        resp = await client.get("/health")
        assert resp.json()["scheduler"] == "running"
        scheduler = cleanup.get_scheduler()
        assert scheduler is not None
        jobs = scheduler.get_jobs()
        assert [job.id for job in jobs] == [CLEANUP_JOB_ID]
        assert jobs[0].trigger.interval == timedelta(minutes=CLEANUP_INTERVAL_MINUTES)
