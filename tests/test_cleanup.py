"""
Tests for the Cleanup Service (Item 5).

Covers:
- Links past their expiry time are soft-deleted
- Links that have hit their click cap are soft-deleted
- Links with no expiry / no cap are left untouched
- Links already soft-deleted are not touched again
- run_cleanup returns the correct count
- The /health endpoint reports scheduler status
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.models.link import Link
from linkvault.models.user import User
from linkvault.services.cleanup import run_cleanup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REGISTER_URL = "/users/register"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _make_user(db: AsyncSession, email: str = "cleanup@example.com") -> User:
    """Insert a minimal user directly into the DB for unit-test purposes."""
    import hashlib
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        password_hash=hashlib.sha256(b"pw").hexdigest(),
        api_key_hash=hashlib.sha256(str(uuid.uuid4()).encode()).hexdigest(),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_link(
    db: AsyncSession,
    user_id: str,
    slug: str,
    expires_at: datetime | None = None,
    max_clicks: int | None = None,
    click_count: int = 0,
    deleted_at: datetime | None = None,
) -> Link:
    link = Link(
        id=str(uuid.uuid4()),
        user_id=user_id,
        slug=slug,
        destination_url="https://example.com",
        expires_at=expires_at,
        max_clicks=max_clicks,
        click_count=click_count,
        deleted_at=deleted_at,
    )
    db.add(link)
    await db.commit()
    await db.refresh(link)
    return link


# ---------------------------------------------------------------------------
# run_cleanup — expired links
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_expires_old_links(db_session: AsyncSession) -> None:
    user = await _make_user(db_session)
    past = _utc_now() - timedelta(hours=1)
    link = await _make_link(db_session, user.id, "expire1", expires_at=past)

    count = await run_cleanup(db_session)

    assert count == 1
    await db_session.refresh(link)
    assert link.deleted_at is not None


@pytest.mark.asyncio
async def test_cleanup_ignores_future_expiry(db_session: AsyncSession) -> None:
    user = await _make_user(db_session, "future@example.com")
    future = _utc_now() + timedelta(hours=1)
    link = await _make_link(db_session, user.id, "future1", expires_at=future)

    count = await run_cleanup(db_session)

    assert count == 0
    await db_session.refresh(link)
    assert link.deleted_at is None


@pytest.mark.asyncio
async def test_cleanup_ignores_no_expiry(db_session: AsyncSession) -> None:
    user = await _make_user(db_session, "noexp@example.com")
    link = await _make_link(db_session, user.id, "noexp1")

    count = await run_cleanup(db_session)

    assert count == 0
    await db_session.refresh(link)
    assert link.deleted_at is None


# ---------------------------------------------------------------------------
# run_cleanup — click-capped links
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_caps_exhausted_links(db_session: AsyncSession) -> None:
    user = await _make_user(db_session, "cap@example.com")
    link = await _make_link(
        db_session, user.id, "cap1", max_clicks=10, click_count=10
    )

    count = await run_cleanup(db_session)

    assert count == 1
    await db_session.refresh(link)
    assert link.deleted_at is not None


@pytest.mark.asyncio
async def test_cleanup_caps_over_limit(db_session: AsyncSession) -> None:
    """click_count > max_clicks should also be cleaned up."""
    user = await _make_user(db_session, "over@example.com")
    link = await _make_link(
        db_session, user.id, "over1", max_clicks=5, click_count=99
    )

    count = await run_cleanup(db_session)

    assert count == 1
    await db_session.refresh(link)
    assert link.deleted_at is not None


@pytest.mark.asyncio
async def test_cleanup_ignores_under_cap(db_session: AsyncSession) -> None:
    user = await _make_user(db_session, "under@example.com")
    link = await _make_link(
        db_session, user.id, "under1", max_clicks=10, click_count=5
    )

    count = await run_cleanup(db_session)

    assert count == 0
    await db_session.refresh(link)
    assert link.deleted_at is None


@pytest.mark.asyncio
async def test_cleanup_ignores_no_cap(db_session: AsyncSession) -> None:
    user = await _make_user(db_session, "nocap@example.com")
    link = await _make_link(db_session, user.id, "nocap1", click_count=9999)

    count = await run_cleanup(db_session)

    assert count == 0
    await db_session.refresh(link)
    assert link.deleted_at is None


# ---------------------------------------------------------------------------
# run_cleanup — already-deleted links are skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_skips_already_deleted(db_session: AsyncSession) -> None:
    user = await _make_user(db_session, "deleted@example.com")
    already_deleted = _utc_now() - timedelta(days=1)
    past = _utc_now() - timedelta(hours=2)
    link = await _make_link(
        db_session,
        user.id,
        "del1",
        expires_at=past,
        deleted_at=already_deleted,
    )
    original_deleted_at = link.deleted_at

    count = await run_cleanup(db_session)

    # Should be 0 — link was already deleted
    assert count == 0
    await db_session.refresh(link)
    assert link.deleted_at == original_deleted_at


# ---------------------------------------------------------------------------
# run_cleanup — mixed batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_mixed_batch(db_session: AsyncSession) -> None:
    user = await _make_user(db_session, "mix@example.com")
    past = _utc_now() - timedelta(hours=1)
    future = _utc_now() + timedelta(hours=1)

    expired = await _make_link(db_session, user.id, "mix1", expires_at=past)
    capped = await _make_link(db_session, user.id, "mix2", max_clicks=1, click_count=1)
    alive = await _make_link(db_session, user.id, "mix3", expires_at=future)
    noexpiry = await _make_link(db_session, user.id, "mix4")

    count = await run_cleanup(db_session)

    assert count == 2

    await db_session.refresh(expired)
    await db_session.refresh(capped)
    await db_session.refresh(alive)
    await db_session.refresh(noexpiry)

    assert expired.deleted_at is not None
    assert capped.deleted_at is not None
    assert alive.deleted_at is None
    assert noexpiry.deleted_at is None


# ---------------------------------------------------------------------------
# /health reports scheduler status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_endpoint_shape(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    # Scheduler may be "running", "stopped", or "error" depending on environment
    assert body["scheduler"] in ("running", "stopped", "error")
    assert "version" in body
