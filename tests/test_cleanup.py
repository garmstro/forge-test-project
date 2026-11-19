"""
Tests for the expired link cleanup job and scheduler (Step 5).

Covers:
- Expired, undeleted links are soft-deleted
- Non-expired links, links without expiry and already-deleted links are untouched
- Re-running cleanup is a no-op
- Structured log line with the correct count (also when 0)
- Failures are caught and logged
- Slug of a cleaned-up link can be reused
- /health reports the scheduler state; persistent job store has no duplicates
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from linkvault.config import settings
from linkvault.main import app
from linkvault.models.link import Link
from linkvault.services.cleanup import CLEANUP_JOB_ID, expire_links, run_cleanup_job

REGISTER_URL = "/users/register"
LINKS_URL = "/links"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory(db_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)


async def register_and_get_key(client: AsyncClient, email: str = "cleanup@example.com") -> str:
    resp = await client.post(REGISTER_URL, json={"email": email, "password": "password123"})
    assert resp.status_code == 201, resp.text
    return resp.json()["api_key"]


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


async def create_link(
    client: AsyncClient, api_key: str, slug: str, expires_at: str | None = None
) -> dict:
    payload: dict = {"url": "https://example.com/x", "slug": slug}
    if expires_at is not None:
        payload["expires_at"] = expires_at
    resp = await client.post(LINKS_URL, json=payload, headers=auth(api_key))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def get_link_row(factory: async_sessionmaker[AsyncSession], link_id: str) -> Link:
    async with factory() as session:
        result = await session.execute(select(Link).where(Link.id == link_id))
        return result.scalar_one()


def cleanup_records(caplog: pytest.LogCaptureFixture) -> list[dict]:
    out = []
    for record in caplog.records:
        if record.name != "linkvault.services.cleanup":
            continue
        try:
            data = json.loads(record.getMessage())
        except ValueError:
            continue
        if data.get("event") == "cleanup":
            out.append(data)
    return out


# ---------------------------------------------------------------------------
# Cleanup behaviour
# ---------------------------------------------------------------------------


async def test_expired_link_is_soft_deleted(client, session_factory):
    key = await register_and_get_key(client)
    link = await create_link(client, key, "gone-soon", expires_at=iso(timedelta(hours=-1)))

    assert await expire_links(session_factory) == 1

    row = await get_link_row(session_factory, link["id"])
    assert row.deleted_at is not None
    # deleted_at is stored as naive UTC, close to now
    assert abs(row.deleted_at - datetime.now(timezone.utc).replace(tzinfo=None)) < timedelta(minutes=1)

    # Running again changes nothing
    assert await expire_links(session_factory) == 0
    row_again = await get_link_row(session_factory, link["id"])
    assert row_again.deleted_at == row.deleted_at


async def test_unexpired_and_no_expiry_links_untouched(client, session_factory):
    key = await register_and_get_key(client)
    future = await create_link(client, key, "future-link", expires_at=iso(timedelta(days=1)))
    forever = await create_link(client, key, "forever-link")

    assert await expire_links(session_factory) == 0

    assert (await get_link_row(session_factory, future["id"])).deleted_at is None
    assert (await get_link_row(session_factory, forever["id"])).deleted_at is None


async def test_already_deleted_link_keeps_original_deleted_at(client, session_factory):
    key = await register_and_get_key(client)
    link = await create_link(client, key, "old-deleted", expires_at=iso(timedelta(hours=-1)))
    resp = await client.delete(f"{LINKS_URL}/old-deleted", headers=auth(key))
    assert resp.status_code in (200, 204), resp.text
    original = (await get_link_row(session_factory, link["id"])).deleted_at
    assert original is not None

    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert await expire_links(session_factory, now=later) == 0
    assert (await get_link_row(session_factory, link["id"])).deleted_at == original


async def test_log_line_reports_count(client, session_factory, caplog):
    key = await register_and_get_key(client)
    await create_link(client, key, "exp-one", expires_at=iso(timedelta(hours=-2)))
    await create_link(client, key, "exp-two", expires_at=iso(timedelta(minutes=-5)))
    await create_link(client, key, "not-exp", expires_at=iso(timedelta(hours=2)))

    with caplog.at_level(logging.INFO, logger="linkvault.services.cleanup"):
        assert await run_cleanup_job(session_factory) == 2
        assert await run_cleanup_job(session_factory) == 0

    records = cleanup_records(caplog)
    assert [r["links_expired"] for r in records] == [2, 0]
    for r in records:
        assert set(r) == {"event", "links_expired", "timestamp"}
        datetime.fromisoformat(r["timestamp"])


async def test_failure_is_caught_and_logged(caplog):
    def broken_factory():
        raise RuntimeError("db is down")

    with caplog.at_level(logging.INFO, logger="linkvault.services.cleanup"):
        assert await run_cleanup_job(broken_factory) == 0  # type: ignore[arg-type]

    failures = [r for r in caplog.records if "cleanup_failed" in r.getMessage()]
    assert len(failures) == 1
    assert failures[0].levelno == logging.ERROR


async def test_slug_reusable_after_cleanup(client, session_factory):
    key = await register_and_get_key(client)
    await create_link(client, key, "reuse-me", expires_at=iso(timedelta(hours=-1)))

    # Still occupied before cleanup
    resp = await client.post(
        LINKS_URL, json={"url": "https://example.com/new", "slug": "reuse-me"}, headers=auth(key)
    )
    assert resp.status_code == 409

    await expire_links(session_factory)

    new_link = await create_link(client, key, "reuse-me")
    assert new_link["deleted_at"] is None
    # Redirect now goes to the new (non-expiring) link
    resp = await client.get("/reuse-me")
    assert resp.status_code == 301


# ---------------------------------------------------------------------------
# Scheduler + health
# ---------------------------------------------------------------------------


async def test_health_reports_stopped_without_scheduler(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["scheduler"] != "running"


async def test_scheduler_lifecycle_and_persistence(tmp_path, monkeypatch):
    db_file = tmp_path / "scheduler.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setattr(settings, "SCHEDULER_DB_URL", url)
    monkeypatch.setattr(settings, "CLEANUP_INTERVAL_MINUTES", 0.5)

    async def health_scheduler() -> str:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/health")
            assert resp.status_code == 200
            return resp.json()["scheduler"]

    # Start and stop twice to simulate a restart.
    for _ in range(2):
        async with app.router.lifespan_context(app):
            assert await health_scheduler() == "running"
            job = app.state.scheduler.get_job(CLEANUP_JOB_ID)
            assert job is not None
            assert job.trigger.interval == timedelta(seconds=30)
        assert await health_scheduler() == "stopped"

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            ids = [row[0] for row in conn.execute(text("SELECT id FROM apscheduler_jobs"))]
    finally:
        engine.dispose()
    assert ids == [CLEANUP_JOB_ID]
