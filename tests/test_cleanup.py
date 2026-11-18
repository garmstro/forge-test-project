"""
Tests for the expired link cleanup job (Item 5).

Covers:
- An expired link is soft-deleted by a cleanup run, then redirects 404
- Non-expired, never-expiring and already-deleted links are untouched
  (already-deleted links keep their original deleted_at)
- A run with nothing to expire succeeds and logs links_expired = 0
- Each run emits exactly one structured JSON log line
- App startup registers exactly one 15-minute job in a persistent SQLite
  job store, and a restart does not create a duplicate
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from linkvault.config import settings
from linkvault.main import app, lifespan
from linkvault.models.link import Link
from linkvault.models.user import User
from linkvault.services.cleanup import (
    CLEANUP_INTERVAL_MINUTES,
    CLEANUP_JOB_ID,
    expire_links,
)

CLEANUP_LOGGER = "linkvault.services.cleanup"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def make_user(factory, email: str = "cleanup@example.com") -> str:
    async with factory() as session:
        user = User(email=email, password_hash="x", api_key_hash=f"hash-{email}")
        session.add(user)
        await session.commit()
        return user.id


async def make_link(
    factory,
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


async def get_link(factory, link_id: str) -> Link:
    async with factory() as session:
        result = await session.execute(select(Link).where(Link.id == link_id))
        return result.scalar_one()


def cleanup_log_lines(caplog: pytest.LogCaptureFixture) -> list[dict]:
    lines = []
    for record in caplog.records:
        if record.name != CLEANUP_LOGGER:
            continue
        payload = json.loads(record.getMessage())
        if payload.get("event") == "cleanup":
            lines.append(payload)
    return lines


# ---------------------------------------------------------------------------
# Job behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_link_is_soft_deleted_and_then_404(
    client: AsyncClient, session_factory
) -> None:
    """End to end: create an expired link via the API, clean up, then 404."""
    reg = await client.post(
        "/users/register", json={"email": "exp@example.com", "password": "password123"}
    )
    key = reg.json()["api_key"]
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    resp = await client.post(
        "/links",
        json={"url": "https://example.com/old", "slug": "old-link", "expires_at": past},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 201, resp.text

    # Before cleanup: expired but not yet purged -> 410.
    assert (await client.get("/old-link", follow_redirects=False)).status_code == 410

    before = utcnow_naive()
    assert await expire_links(session_factory) == 1

    async with session_factory() as session:
        link = (
            await session.execute(select(Link).where(Link.slug == "old-link"))
        ).scalar_one()
    assert link.deleted_at is not None
    assert link.deleted_at >= before - timedelta(seconds=1)

    # After cleanup the link is soft-deleted -> 404 not_found.
    after = await client.get("/old-link", follow_redirects=False)
    assert after.status_code == 404
    assert after.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_non_expired_links_are_untouched(session_factory) -> None:
    user_id = await make_user(session_factory)
    now = utcnow_naive()
    original_deleted_at = now - timedelta(days=3)

    expired_id = await make_link(session_factory, user_id, "gone", expires_at=now - timedelta(hours=1))
    future_id = await make_link(session_factory, user_id, "future", expires_at=now + timedelta(days=1))
    forever_id = await make_link(session_factory, user_id, "forever", expires_at=None)
    already_id = await make_link(
        session_factory,
        user_id,
        "already",
        expires_at=now - timedelta(days=5),
        deleted_at=original_deleted_at,
    )

    assert await expire_links(session_factory) == 1

    assert (await get_link(session_factory, expired_id)).deleted_at is not None
    assert (await get_link(session_factory, future_id)).deleted_at is None
    assert (await get_link(session_factory, forever_id)).deleted_at is None
    # Already-deleted link keeps its original deletion time.
    assert (await get_link(session_factory, already_id)).deleted_at == original_deleted_at


@pytest.mark.asyncio
async def test_cleanup_with_nothing_to_expire_logs_zero(
    session_factory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=CLEANUP_LOGGER)

    assert await expire_links(session_factory) == 0

    lines = cleanup_log_lines(caplog)
    assert len(lines) == 1
    assert lines[0]["links_expired"] == 0


@pytest.mark.asyncio
async def test_cleanup_emits_one_structured_log_line(
    session_factory, caplog: pytest.LogCaptureFixture
) -> None:
    user_id = await make_user(session_factory)
    now = utcnow_naive()
    for i in range(3):
        await make_link(session_factory, user_id, f"exp-{i}", expires_at=now - timedelta(minutes=i + 1))
    await make_link(session_factory, user_id, "keep", expires_at=now + timedelta(hours=1))

    caplog.set_level(logging.INFO, logger=CLEANUP_LOGGER)
    assert await expire_links(session_factory) == 3

    lines = cleanup_log_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert set(line) == {"event", "links_expired", "timestamp"}
    assert line["event"] == "cleanup"
    assert line["links_expired"] == 3
    ts = datetime.fromisoformat(line["timestamp"].replace("Z", "+00:00"))
    assert ts.tzinfo is not None
    assert abs(datetime.now(timezone.utc) - ts) < timedelta(minutes=1)


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------


def stored_job_ids(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        return [row[0] for row in conn.execute("SELECT id FROM apscheduler_jobs")]


@pytest.mark.asyncio
async def test_startup_registers_single_job_and_restart_does_not_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "scheduler.db"
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", True)
    monkeypatch.setattr(settings, "SCHEDULER_DB_URL", f"sqlite:///{db_path}")

    for _ in range(2):  # start, stop, start again
        async with lifespan(app):
            scheduler = app.state.scheduler
            assert scheduler is not None and scheduler.running
            jobs = scheduler.get_jobs()
            assert [job.id for job in jobs] == [CLEANUP_JOB_ID]
            job = jobs[0]
            assert job.trigger.interval == timedelta(minutes=CLEANUP_INTERVAL_MINUTES)
            assert job.func_ref == "linkvault.services.cleanup:run_cleanup_job"
        # Shutdown is scheduled on the event loop; let it complete.
        for _ in range(3):
            await asyncio.sleep(0)
        assert app.state.scheduler is None

    # The job is persisted in the SQLite job store exactly once.
    assert stored_job_ids(db_path) == [CLEANUP_JOB_ID]
