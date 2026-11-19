"""Tests for the cleanup job."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.models.link import Link
from linkvault.models.user import User
from linkvault.services.cleanup import cleanup_expired_links


def _utc_now() -> datetime:
    """Return current UTC time as a naive datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
async def test_user(db_session: AsyncSession) -> User:
    """Create a test user."""
    user = User(
        id="test-user-1",
        email="test@example.com",
        password_hash="hashed_password",
        api_key_hash="hashed_api_key",
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.mark.asyncio
async def test_cleanup_expired_links_basic(
    db_session: AsyncSession, test_user: User, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that cleanup_expired_links marks expired links as deleted."""
    now = _utc_now()

    # Create an expired link (expires_at in the past)
    expired_link = Link(
        id="expired-1",
        user_id=test_user.id,
        slug="expired",
        destination_url="https://example.com",
        expires_at=now - timedelta(hours=1),
        deleted_at=None,
    )

    # Create an active link (expires_at in the future)
    active_link = Link(
        id="active-1",
        user_id=test_user.id,
        slug="active",
        destination_url="https://example.com",
        expires_at=now + timedelta(hours=1),
        deleted_at=None,
    )

    # Create a link with no expiration
    no_expiry_link = Link(
        id="no-expiry-1",
        user_id=test_user.id,
        slug="no-expiry",
        destination_url="https://example.com",
        expires_at=None,
        deleted_at=None,
    )

    db_session.add_all([expired_link, active_link, no_expiry_link])
    await db_session.commit()

    # Run cleanup
    with caplog.at_level(logging.INFO):
        await cleanup_expired_links(db_session)

    # Verify the expired link is marked as deleted
    result = await db_session.execute(select(Link).where(Link.id == "expired-1"))
    updated_expired = result.scalar_one()
    assert updated_expired.deleted_at is not None
    assert updated_expired.deleted_at >= now

    # Verify the active link is NOT marked as deleted
    result = await db_session.execute(select(Link).where(Link.id == "active-1"))
    updated_active = result.scalar_one()
    assert updated_active.deleted_at is None

    # Verify the no-expiry link is NOT marked as deleted
    result = await db_session.execute(select(Link).where(Link.id == "no-expiry-1"))
    updated_no_expiry = result.scalar_one()
    assert updated_no_expiry.deleted_at is None

    # Verify structured JSON log
    assert any("cleanup" in record.message for record in caplog.records)
    log_record = next(r for r in caplog.records if "cleanup" in r.message)
    log_data = json.loads(log_record.message)
    assert log_data["event"] == "cleanup"
    assert log_data["links_expired"] == 1
    assert "timestamp" in log_data


@pytest.mark.asyncio
async def test_cleanup_multiple_expired_links(
    db_session: AsyncSession, test_user: User, caplog: pytest.LogCaptureFixture
) -> None:
    """Test cleanup with multiple expired links."""
    now = _utc_now()

    # Create multiple expired links
    expired_links = [
        Link(
            id=f"expired-{i}",
            user_id=test_user.id,
            slug=f"expired-{i}",
            destination_url="https://example.com",
            expires_at=now - timedelta(hours=i + 1),
            deleted_at=None,
        )
        for i in range(5)
    ]

    db_session.add_all(expired_links)
    await db_session.commit()

    # Run cleanup
    with caplog.at_level(logging.INFO):
        await cleanup_expired_links(db_session)

    # Verify all expired links are marked as deleted
    for i in range(5):
        result = await db_session.execute(
            select(Link).where(Link.id == f"expired-{i}")
        )
        link = result.scalar_one()
        assert link.deleted_at is not None

    # Verify log shows correct count
    log_record = next(r for r in caplog.records if "cleanup" in r.message)
    log_data = json.loads(log_record.message)
    assert log_data["links_expired"] == 5


@pytest.mark.asyncio
async def test_cleanup_no_expired_links(
    db_session: AsyncSession, test_user: User, caplog: pytest.LogCaptureFixture
) -> None:
    """Test cleanup when there are no expired links."""
    now = _utc_now()

    # Create only active links
    active_links = [
        Link(
            id=f"active-{i}",
            user_id=test_user.id,
            slug=f"active-{i}",
            destination_url="https://example.com",
            expires_at=now + timedelta(hours=i + 1),
            deleted_at=None,
        )
        for i in range(3)
    ]

    db_session.add_all(active_links)
    await db_session.commit()

    # Run cleanup
    with caplog.at_level(logging.INFO):
        await cleanup_expired_links(db_session)

    # Verify no links are marked as deleted
    for i in range(3):
        result = await db_session.execute(
            select(Link).where(Link.id == f"active-{i}")
        )
        link = result.scalar_one()
        assert link.deleted_at is None

    # Verify log shows 0 expired links
    log_record = next(r for r in caplog.records if "cleanup" in r.message)
    log_data = json.loads(log_record.message)
    assert log_data["links_expired"] == 0


@pytest.mark.asyncio
async def test_cleanup_ignores_already_deleted_links(
    db_session: AsyncSession, test_user: User, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that cleanup ignores links that are already soft-deleted."""
    now = _utc_now()

    # Create an expired link that's already soft-deleted
    already_deleted = Link(
        id="already-deleted-1",
        user_id=test_user.id,
        slug="already-deleted",
        destination_url="https://example.com",
        expires_at=now - timedelta(hours=1),
        deleted_at=now - timedelta(hours=2),
    )

    # Create an expired link that's not yet deleted
    not_yet_deleted = Link(
        id="not-yet-deleted-1",
        user_id=test_user.id,
        slug="not-yet-deleted",
        destination_url="https://example.com",
        expires_at=now - timedelta(hours=1),
        deleted_at=None,
    )

    db_session.add_all([already_deleted, not_yet_deleted])
    await db_session.commit()

    # Run cleanup
    with caplog.at_level(logging.INFO):
        await cleanup_expired_links(db_session)

    # Verify only the non-deleted link is updated
    log_record = next(r for r in caplog.records if "cleanup" in r.message)
    log_data = json.loads(log_record.message)
    assert log_data["links_expired"] == 1

    # Verify the already-deleted link's deleted_at is unchanged
    result = await db_session.execute(
        select(Link).where(Link.id == "already-deleted-1")
    )
    link = result.scalar_one()
    assert link.deleted_at == now - timedelta(hours=2)


@pytest.mark.asyncio
async def test_cleanup_edge_case_expires_at_equals_now(
    db_session: AsyncSession, test_user: User, caplog: pytest.LogCaptureFixture
) -> None:
    """Test cleanup with expires_at exactly equal to now (should NOT be expired)."""
    now = _utc_now()

    # Create a link that expires exactly now
    link = Link(
        id="expires-now-1",
        user_id=test_user.id,
        slug="expires-now",
        destination_url="https://example.com",
        expires_at=now,
        deleted_at=None,
    )

    db_session.add(link)
    await db_session.commit()

    # Run cleanup
    with caplog.at_level(logging.INFO):
        await cleanup_expired_links(db_session)

    # Verify the link is NOT marked as deleted (expires_at < now is false when equal)
    result = await db_session.execute(select(Link).where(Link.id == "expires-now-1"))
    updated_link = result.scalar_one()
    assert updated_link.deleted_at is None

    # Verify log shows 0 expired links
    log_record = next(r for r in caplog.records if "cleanup" in r.message)
    log_data = json.loads(log_record.message)
    assert log_data["links_expired"] == 0


@pytest.mark.asyncio
async def test_cleanup_json_log_format(
    db_session: AsyncSession, test_user: User, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that cleanup logs valid JSON with required fields."""
    now = _utc_now()

    # Create an expired link
    expired_link = Link(
        id="expired-1",
        user_id=test_user.id,
        slug="expired",
        destination_url="https://example.com",
        expires_at=now - timedelta(hours=1),
        deleted_at=None,
    )

    db_session.add(expired_link)
    await db_session.commit()

    # Run cleanup
    with caplog.at_level(logging.INFO):
        await cleanup_expired_links(db_session)

    # Find and parse the log record
    log_record = next(r for r in caplog.records if "cleanup" in r.message)
    log_data = json.loads(log_record.message)

    # Verify required fields
    assert "event" in log_data
    assert log_data["event"] == "cleanup"
    assert "links_expired" in log_data
    assert isinstance(log_data["links_expired"], int)
    assert "timestamp" in log_data
    # Verify timestamp is ISO format
    datetime.fromisoformat(log_data["timestamp"])
