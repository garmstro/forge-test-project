"""Tests for expired link cleanup job."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from linkvault.models.link import Link
from linkvault.models.user import User
from linkvault.services.cleanup import cleanup_expired_links


@pytest.mark.asyncio
async def test_cleanup_expired_links(db_session, caplog):
    """Test that cleanup_expired_links soft-deletes expired links."""
    # Create a test user
    user = User(
        id="test-user-id",
        email="test@example.com",
        password_hash="fakehash123",
        api_key_hash="fakeapikey123",
    )
    db_session.add(user)
    await db_session.commit()
    
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    past = now - timedelta(hours=1)
    future = now + timedelta(hours=1)
    
    # Create test links with different expiration states
    expired_link_1 = Link(
        id="expired-1",
        user_id=user.id,
        slug="expired1",
        destination_url="https://example.com/expired1",
        expires_at=past,
        deleted_at=None,
    )
    
    expired_link_2 = Link(
        id="expired-2",
        user_id=user.id,
        slug="expired2",
        destination_url="https://example.com/expired2",
        expires_at=past,
        deleted_at=None,
    )
    
    # Link that expires in the future (should not be deleted)
    future_link = Link(
        id="future-1",
        user_id=user.id,
        slug="future1",
        destination_url="https://example.com/future",
        expires_at=future,
        deleted_at=None,
    )
    
    # Link with no expiration (should not be deleted)
    no_expiry_link = Link(
        id="no-expiry-1",
        user_id=user.id,
        slug="noexpiry1",
        destination_url="https://example.com/noexpiry",
        expires_at=None,
        deleted_at=None,
    )
    
    # Already deleted link (should not be touched)
    already_deleted = Link(
        id="already-deleted",
        user_id=user.id,
        slug="deleted1",
        destination_url="https://example.com/deleted",
        expires_at=past,
        deleted_at=now,
    )
    
    db_session.add_all([
        expired_link_1,
        expired_link_2,
        future_link,
        no_expiry_link,
        already_deleted,
    ])
    await db_session.commit()
    
    # Run the cleanup job
    await cleanup_expired_links(db_session)
    
    # Verify that only the expired links were soft-deleted
    stmt = select(Link).where(Link.id == "expired-1")
    result = await db_session.execute(stmt)
    link = result.scalar_one()
    assert link.deleted_at is not None, "Expired link 1 should be soft-deleted"
    
    stmt = select(Link).where(Link.id == "expired-2")
    result = await db_session.execute(stmt)
    link = result.scalar_one()
    assert link.deleted_at is not None, "Expired link 2 should be soft-deleted"
    
    stmt = select(Link).where(Link.id == "future-1")
    result = await db_session.execute(stmt)
    link = result.scalar_one()
    assert link.deleted_at is None, "Future link should not be deleted"
    
    stmt = select(Link).where(Link.id == "no-expiry-1")
    result = await db_session.execute(stmt)
    link = result.scalar_one()
    assert link.deleted_at is None, "No-expiry link should not be deleted"
    
    stmt = select(Link).where(Link.id == "already-deleted")
    result = await db_session.execute(stmt)
    link = result.scalar_one()
    assert link.deleted_at == now, "Already deleted link should not be modified"
    
    # Verify the structured log message
    assert any("cleanup" in record.message for record in caplog.records)
    
    # Parse and verify the JSON log message
    log_found = False
    for record in caplog.records:
        if "cleanup" in record.message:
            try:
                log_data = json.loads(record.message)
                assert log_data["event"] == "cleanup"
                assert log_data["links_expired"] == 2
                assert "timestamp" in log_data
                log_found = True
                break
            except json.JSONDecodeError:
                continue
    
    assert log_found, "Structured JSON log message not found"


@pytest.mark.asyncio
async def test_cleanup_no_expired_links(db_session, caplog):
    """Test cleanup when there are no expired links."""
    # Create a test user
    user = User(
        id="test-user-id",
        email="test@example.com",
        password_hash="fakehash123",
        api_key_hash="fakeapikey123",
    )
    db_session.add(user)
    await db_session.commit()
    
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    future = now + timedelta(hours=1)
    
    # Create a link that hasn't expired
    future_link = Link(
        id="future-1",
        user_id=user.id,
        slug="future1",
        destination_url="https://example.com/future",
        expires_at=future,
        deleted_at=None,
    )
    
    db_session.add(future_link)
    await db_session.commit()
    
    # Run the cleanup job
    await cleanup_expired_links(db_session)
    
    # Verify the link wasn't deleted
    stmt = select(Link).where(Link.id == "future-1")
    result = await db_session.execute(stmt)
    link = result.scalar_one()
    assert link.deleted_at is None, "Future link should not be deleted"
    
    # Verify the log shows 0 links expired
    log_found = False
    for record in caplog.records:
        if "cleanup" in record.message:
            try:
                log_data = json.loads(record.message)
                assert log_data["event"] == "cleanup"
                assert log_data["links_expired"] == 0
                log_found = True
                break
            except json.JSONDecodeError:
                continue
    
    assert log_found, "Structured JSON log message not found"


@pytest.mark.asyncio
async def test_cleanup_handles_db_error(db_session, caplog, monkeypatch):
    """Test that cleanup handles database errors gracefully."""
    # Create a test user
    user = User(
        id="test-user-id",
        email="test@example.com",
        password_hash="fakehash123",
        api_key_hash="fakeapikey123",
    )
    db_session.add(user)
    await db_session.commit()
    
    # Monkeypatch the execute method to raise an exception
    async def mock_execute(*args, **kwargs):
        raise RuntimeError("Database connection lost")
    
    monkeypatch.setattr(db_session, "execute", mock_execute)
    
    # Run cleanup — should catch and log the exception
    with pytest.raises(RuntimeError, match="Database connection lost"):
        await cleanup_expired_links(db_session)
    
    # Verify error was logged
    assert any("Error during expired link cleanup" in record.message for record in caplog.records)
