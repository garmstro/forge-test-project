"""Tests for the background cleanup job."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.models.link import Link
from linkvault.models.user import User
from linkvault.services.cleanup import cleanup_expired_links


def _utc_now() -> datetime:
    """Return the current UTC time as a naive datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _create_test_user(email: str = "test@example.com") -> User:
    """Helper to create a test user with all required fields."""
    return User(
        email=email,
        password_hash="hash",
        api_key_hash=str(uuid.uuid4()),
    )


@pytest.mark.asyncio
async def test_cleanup_expired_links_marks_expired_as_deleted(
    db_session: AsyncSession,
) -> None:
    """Test that cleanup_expired_links marks expired links as deleted."""
    # Create a user
    user = _create_test_user()
    db_session.add(user)
    await db_session.flush()

    now = _utc_now()
    past = now - timedelta(hours=1)
    future = now + timedelta(hours=1)

    # Create an expired link (expires_at in the past)
    expired_link = Link(
        user_id=user.id,
        slug="expired",
        destination_url="https://example.com/expired",
        expires_at=past,
        deleted_at=None,
    )

    # Create an active link (expires_at in the future)
    active_link = Link(
        user_id=user.id,
        slug="active",
        destination_url="https://example.com/active",
        expires_at=future,
        deleted_at=None,
    )

    # Create a link with no expiry
    no_expiry_link = Link(
        user_id=user.id,
        slug="no-expiry",
        destination_url="https://example.com/no-expiry",
        expires_at=None,
        deleted_at=None,
    )

    db_session.add_all([expired_link, active_link, no_expiry_link])
    await db_session.commit()

    # Run cleanup
    await cleanup_expired_links(db_session)

    # Refresh from DB
    await db_session.refresh(expired_link)
    await db_session.refresh(active_link)
    await db_session.refresh(no_expiry_link)

    # Verify: expired link is deleted, others are not
    assert expired_link.deleted_at is not None
    assert active_link.deleted_at is None
    assert no_expiry_link.deleted_at is None


@pytest.mark.asyncio
async def test_cleanup_expired_links_ignores_already_deleted(
    db_session: AsyncSession,
) -> None:
    """Test that cleanup_expired_links does not re-delete already-deleted links."""
    # Create a user
    user = _create_test_user()
    db_session.add(user)
    await db_session.flush()

    now = _utc_now()
    past = now - timedelta(hours=1)

    # Create an expired link that is already deleted
    already_deleted = Link(
        user_id=user.id,
        slug="already-deleted",
        destination_url="https://example.com/already-deleted",
        expires_at=past,
        deleted_at=past - timedelta(hours=1),
    )

    db_session.add(already_deleted)
    await db_session.commit()

    original_deleted_at = already_deleted.deleted_at

    # Run cleanup
    await cleanup_expired_links(db_session)

    # Refresh from DB
    await db_session.refresh(already_deleted)

    # Verify: deleted_at should not have changed
    assert already_deleted.deleted_at == original_deleted_at


@pytest.mark.asyncio
async def test_cleanup_expired_links_handles_multiple_expired(
    db_session: AsyncSession,
) -> None:
    """Test that cleanup_expired_links handles multiple expired links."""
    # Create a user
    user = _create_test_user()
    db_session.add(user)
    await db_session.flush()

    now = _utc_now()
    past = now - timedelta(hours=1)

    # Create multiple expired links
    links = [
        Link(
            user_id=user.id,
            slug=f"expired-{i}",
            destination_url=f"https://example.com/expired-{i}",
            expires_at=past,
            deleted_at=None,
        )
        for i in range(5)
    ]

    db_session.add_all(links)
    await db_session.commit()

    # Run cleanup
    await cleanup_expired_links(db_session)

    # Verify: all are deleted
    for link in links:
        await db_session.refresh(link)
        assert link.deleted_at is not None


@pytest.mark.asyncio
async def test_cleanup_expired_links_logs_structured_json(
    db_session: AsyncSession,
    caplog,
) -> None:
    """Test that cleanup_expired_links logs structured JSON."""
    import logging

    # Set up logging to capture INFO level
    caplog.set_level(logging.INFO)

    # Create a user
    user = _create_test_user()
    db_session.add(user)
    await db_session.flush()

    now = _utc_now()
    past = now - timedelta(hours=1)

    # Create 3 expired links
    links = [
        Link(
            user_id=user.id,
            slug=f"expired-{i}",
            destination_url=f"https://example.com/expired-{i}",
            expires_at=past,
            deleted_at=None,
        )
        for i in range(3)
    ]

    db_session.add_all(links)
    await db_session.commit()

    # Run cleanup
    await cleanup_expired_links(db_session)

    # Find the log entry with the cleanup event
    cleanup_logs = [
        record for record in caplog.records
        if "cleanup" in record.message
    ]

    assert len(cleanup_logs) > 0, "No cleanup log found"

    # Parse the JSON from the log message
    log_message = cleanup_logs[0].message
    log_data = json.loads(log_message)

    # Verify structure
    assert log_data["event"] == "cleanup"
    assert log_data["links_expired"] == 3
    assert "timestamp" in log_data


@pytest.mark.asyncio
async def test_cleanup_expired_links_logs_zero_when_none_expired(
    db_session: AsyncSession,
    caplog,
) -> None:
    """Test that cleanup_expired_links logs zero when no links are expired."""
    import logging

    caplog.set_level(logging.INFO)

    # Create a user
    user = _create_test_user()
    db_session.add(user)
    await db_session.flush()

    now = _utc_now()
    future = now + timedelta(hours=1)

    # Create only active links
    link = Link(
        user_id=user.id,
        slug="active",
        destination_url="https://example.com/active",
        expires_at=future,
        deleted_at=None,
    )

    db_session.add(link)
    await db_session.commit()

    # Run cleanup
    await cleanup_expired_links(db_session)

    # Find the log entry
    cleanup_logs = [
        record for record in caplog.records
        if "cleanup" in record.message
    ]

    assert len(cleanup_logs) > 0

    log_message = cleanup_logs[0].message
    log_data = json.loads(log_message)

    # Verify: links_expired should be 0
    assert log_data["event"] == "cleanup"
    assert log_data["links_expired"] == 0
