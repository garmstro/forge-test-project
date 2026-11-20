"""
Tests for the expired link cleanup job and scheduler (Item 5).

Covers:
- Expired, non-deleted links are soft-deleted by a cleanup run
- Unexpired links, links without expiry and already-deleted links are untouched
- One structured log line per run with the correct count (0 when idle)
- Expired links disappear from GET /links and their slug becomes reusable
- A failing run is logged and does not raise
- Scheduler starts, registers the job every 15 minutes, and restarting with
  the same persistent job store does not create duplicate jobs
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from linkvault.models.link import Link
from linkvault.models.user import User
from linkvault.services import cleanup
from linkvault.services.cleanup import (
    CLEANUP_JOB_ID,
    is_scheduler_running,
    purge_expired_links,
    run_cleanup_job,
    shutdown_scheduler,
    start_scheduler,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def make_user(session: AsyncSession) -> User:
    user = User(email="owner@example.com", password_hash="x", api_key_hash="y")
    session.add(user)
    await session.commit()
    return user


def cleanup_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    out = []
    for rec in caplog.records:
        if rec.name != "linkvault.services.cleanup":
            continue
        try:
            payload = json.loads(rec.getMessage())
        except ValueError:
            continue
        if payload.get("event") == "cleanup":
            out.append(payload)
    return out


# ---------------------------------------------------------------------------
# purge_expired_links
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_expires_only_eligible_links(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    user = await make_user(db_session)
    now = utcnow()
    earlier_deleted_at = now - timedelta(days=3)
    links = {
        "expired": Link(user_id=user.id, slug="expired", destination_url="https://a.com",
                        expires_at=now - timedelta(minutes=1)),
        "expired2": Link(user_id=user.id, slug="expired2", destination_url="https://a.com",
                         expires_at=now - timedelta(days=10)),
        "future": Link(user_id=user.id, slug="future", destination_url="https://a.com",
                       expires_at=now + timedelta(days=1)),
        "forever": Link(user_id=user.id, slug="forever", destination_url="https://a.com"),
        "gone": Link(user_id=user.id, slug="gone", destination_url="https://a.com",
                     expires_at=now - timedelta(days=5), deleted_at=earlier_deleted_at),
    }
    db_session.add_all(links.values())
    await db_session.commit()

    caplog.set_level(logging.INFO, logger="linkvault.services.cleanup")
    count = await purge_expired_links(db_session)
    assert count == 2

    db_session.expire_all()
    rows = {
        link.slug: link
        for link in (await db_session.execute(select(Link))).scalars().all()
    }
    assert rows["expired"].deleted_at is not None
    assert rows["expired2"].deleted_at is not None
    assert rows["expired"].deleted_at >= now
    assert rows["future"].deleted_at is None
    assert rows["forever"].deleted_at is None
    # Already-deleted link keeps its original deletion time.
    assert rows["gone"].deleted_at == earlier_deleted_at

    records = cleanup_records(caplog)
    assert len(records) == 1
    assert records[0]["links_expired"] == 2
    assert isinstance(records[0]["timestamp"], str)
    datetime.fromisoformat(str(records[0]["timestamp"]))


@pytest.mark.asyncio
async def test_cleanup_with_nothing_to_do_reports_zero(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="linkvault.services.cleanup")
    assert await purge_expired_links(db_session) == 0
    records = cleanup_records(caplog)
    assert len(records) == 1
    assert records[0]["links_expired"] == 0


@pytest.mark.asyncio
async def test_run_cleanup_job_uses_session_factory(db_engine, db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    db_session.add(Link(user_id=user.id, slug="old", destination_url="https://a.com",
                        expires_at=utcnow() - timedelta(hours=1)))
    await db_session.commit()

    factory = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)
    assert await run_cleanup_job(factory) == 1
    # Second run has nothing left to do.
    assert await run_cleanup_job(factory) == 0


@pytest.mark.asyncio
async def test_run_cleanup_job_failure_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenSession:
        async def __aenter__(self) -> BrokenSession:
            raise RuntimeError("database is on fire")

        async def __aexit__(self, *exc: object) -> None:
            return None

    def broken_factory() -> BrokenSession:
        return BrokenSession()

    caplog.set_level(logging.INFO, logger="linkvault.services.cleanup")
    result = await run_cleanup_job(broken_factory)  # type: ignore[arg-type]
    assert result is None
    failures = [r for r in caplog.records if "cleanup_failed" in r.getMessage()]
    assert len(failures) == 1
    assert "database is on fire" in failures[0].getMessage()


# ---------------------------------------------------------------------------
# End-to-end via the API: expired link vanishes and its slug is reusable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_link_hidden_and_slug_reusable(
    client: AsyncClient, db_engine
) -> None:
    resp = await client.post(
        "/users/register", json={"email": "e2e@example.com", "password": "password123"}
    )
    headers = {"Authorization": f"Bearer {resp.json()['api_key']}"}

    resp = await client.post(
        "/links",
        json={"url": "https://example.com/old", "slug": "promo",
              "expires_at": "2020-01-01T00:00:00Z"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text

    factory = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)
    assert await run_cleanup_job(factory) == 1

    listing = await client.get("/links", headers=headers)
    assert listing.status_code == 200
    assert all(item["slug"] != "promo" for item in listing.json()["items"])

    resp = await client.post(
        "/links", json={"url": "https://example.com/new", "slug": "promo"}, headers=headers
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture()
def jobstore_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'scheduler.db'}"


@pytest.mark.asyncio
async def test_scheduler_start_registers_job(jobstore_url: str) -> None:
    try:
        scheduler = start_scheduler(jobstore_url)
        assert is_scheduler_running()
        jobs = scheduler.get_jobs()
        assert [job.id for job in jobs] == [CLEANUP_JOB_ID]
        assert jobs[0].trigger.interval == timedelta(minutes=15)
    finally:
        shutdown_scheduler()
    assert not is_scheduler_running()


@pytest.mark.asyncio
async def test_scheduler_restart_does_not_duplicate_job(jobstore_url: str) -> None:
    try:
        start_scheduler(jobstore_url)
        shutdown_scheduler()
        scheduler = start_scheduler(jobstore_url)
        assert len(scheduler.get_jobs()) == 1
        # Starting again while running is a no-op.
        assert start_scheduler(jobstore_url) is scheduler
        assert len(scheduler.get_jobs()) == 1
    finally:
        shutdown_scheduler()


@pytest.mark.asyncio
async def test_app_lifespan_starts_and_stops_scheduler(
    jobstore_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from linkvault.main import app

    monkeypatch.setattr(cleanup.settings, "SCHEDULER_DB_URL", jobstore_url)
    async with app.router.lifespan_context(app):
        assert is_scheduler_running()
    assert not is_scheduler_running()
