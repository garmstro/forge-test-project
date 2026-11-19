"""Tests for the expired link cleanup background job."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from linkvault.models.link import Link
from linkvault.services.cleanup import cleanup_expired_links


@pytest.mark.asyncio
async def test_cleanup_expired_links_updates_deleted_at(db_session):
    """Test that cleanup_expired_links sets deleted_at for expired links."""
    # Create test user
    from linkvault.models.user import User
    user = User(
        id="test-user-id",
        email="test@example.com",
        password_hash="fake",
        api_key_hash="fake-key-hash",
    )
    db_session.add(user)
    await db_session.commit()

    # Create an expired link (expires_at is in the past)
    expired_link = Link(
        id="link-expired",
        user_id=user.id,
        slug="expired",
        destination_url="https://example.com/expired",
        expires_at=datetime.utcnow() - timedelta(hours=1),
        deleted_at=None,
    )
    db_session.add(expired_link)

    # Create a non-expired link
    active_link = Link(
        id="link-active",
        user_id=user.id,
        slug="active",
        destination_url="https://example.com/active",
        expires_at=datetime.utcnow() + timedelta(hours=1),
        deleted_at=None,
    )
    db_session.add(active_link)

    # Create a link with no expiration
    permanent_link = Link(
        id="link-permanent",
        user_id=user.id,
        slug="permanent",
        destination_url="https://example.com/permanent",
        expires_at=None,
        deleted_at=None,
    )
    db_session.add(permanent_link)

    await db_session.commit()

    # Patch AsyncSessionLocal to use our test session
    with patch("linkvault.services.cleanup.AsyncSessionLocal") as mock_session_local:
        mock_session_local.return_value.__aenter__.return_value = db_session

        # Run cleanup
        await cleanup_expired_links()

    # Refresh the objects from database
    await db_session.refresh(expired_link)
    await db_session.refresh(active_link)
    await db_session.refresh(permanent_link)

    # Verify that only the expired link was marked as deleted
    assert expired_link.deleted_at is not None
    assert active_link.deleted_at is None
    assert permanent_link.deleted_at is None


@pytest.mark.asyncio
async def test_cleanup_expired_links_skips_already_deleted(db_session):
    """Test that cleanup_expired_links doesn't update already-deleted expired links."""
    # Create test user
    from linkvault.models.user import User
    user = User(
        id="test-user-id",
        email="test@example.com",
        password_hash="fake",
        api_key_hash="fake-key-hash",
    )
    db_session.add(user)
    await db_session.commit()

    # Create an expired link that was already soft-deleted
    already_deleted_time = datetime.utcnow() - timedelta(days=2)
    already_deleted_link = Link(
        id="link-already-deleted",
        user_id=user.id,
        slug="already-deleted",
        destination_url="https://example.com/deleted",
        expires_at=datetime.utcnow() - timedelta(hours=1),
        deleted_at=already_deleted_time,
    )
    db_session.add(already_deleted_link)
    await db_session.commit()

    # Patch AsyncSessionLocal to use our test session
    with patch("linkvault.services.cleanup.AsyncSessionLocal") as mock_session_local:
        mock_session_local.return_value.__aenter__.return_value = db_session

        # Run cleanup
        await cleanup_expired_links()

    # Refresh the object from database
    await db_session.refresh(already_deleted_link)

    # Verify that deleted_at was NOT changed
    assert already_deleted_link.deleted_at == already_deleted_time


@pytest.mark.asyncio
async def test_cleanup_expired_links_logs_json(db_session, caplog):
    """Test that cleanup_expired_links logs a structured JSON event."""
    # Create test user
    from linkvault.models.user import User
    user = User(
        id="test-user-id",
        email="test@example.com",
        password_hash="fake",
        api_key_hash="fake-key-hash",
    )
    db_session.add(user)
    await db_session.commit()

    # Create two expired links
    for i in range(2):
        link = Link(
            id=f"link-{i}",
            user_id=user.id,
            slug=f"expired-{i}",
            destination_url=f"https://example.com/{i}",
            expires_at=datetime.utcnow() - timedelta(hours=1),
            deleted_at=None,
        )
        db_session.add(link)
    await db_session.commit()

    # Patch AsyncSessionLocal to use our test session
    with patch("linkvault.services.cleanup.AsyncSessionLocal") as mock_session_local:
        mock_session_local.return_value.__aenter__.return_value = db_session

        # Run cleanup with logging capture
        with caplog.at_level("INFO"):
            await cleanup_expired_links()

    # Verify the log contains a JSON message with the expected structure
    assert len(caplog.records) == 1
    log_message = caplog.records[0].message
    log_data = json.loads(log_message)

    assert log_data["event"] == "cleanup"
    assert log_data["links_expired"] == 2
    assert "timestamp" in log_data
    # Verify timestamp is a valid ISO format datetime
    datetime.fromisoformat(log_data["timestamp"])


@pytest.mark.asyncio
async def test_cleanup_expired_links_no_expired_links(db_session, caplog):
    """Test that cleanup works correctly when there are no expired links."""
    # Patch AsyncSessionLocal to use our test session
    with patch("linkvault.services.cleanup.AsyncSessionLocal") as mock_session_local:
        mock_session_local.return_value.__aenter__.return_value = db_session

        # Run cleanup with logging capture
        with caplog.at_level("INFO"):
            await cleanup_expired_links()

    # Verify the log shows 0 links expired
    assert len(caplog.records) == 1
    log_message = caplog.records[0].message
    log_data = json.loads(log_message)

    assert log_data["event"] == "cleanup"
    assert log_data["links_expired"] == 0
    assert "timestamp" in log_data
