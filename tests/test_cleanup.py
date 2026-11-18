"""Tests for the background cleanup job."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from linkvault.models.link import Link
from linkvault.models.user import User
from linkvault.services.cleanup import expire_links_job


@pytest.mark.asyncio
async def test_cleanup_expires_old_links(db_session):
    """Test that the cleanup job soft-deletes expired links."""
    # Create a test user
    user = User(
        email="test@example.com",
        password_hash="dummy",
        api_key_hash="dummy",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    
    # Create an expired link
    expired_time = datetime.now(timezone.utc) - timedelta(hours=1)
    expired_link = Link(
        user_id=user.id,
        slug="expired",
        destination_url="https://example.com/expired",
        expires_at=expired_time,
    )
    
    # Create a future-expiring link
    future_time = datetime.now(timezone.utc) + timedelta(hours=1)
    future_link = Link(
        user_id=user.id,
        slug="future",
        destination_url="https://example.com/future",
        expires_at=future_time,
    )
    
    # Create a link with no expiry
    no_expiry_link = Link(
        user_id=user.id,
        slug="permanent",
        destination_url="https://example.com/permanent",
    )
    
    db_session.add_all([expired_link, future_link, no_expiry_link])
    await db_session.commit()
    
    # Run the cleanup job with the test session
    await expire_links_job(session=db_session)
    
    # Refresh and check results
    await db_session.refresh(expired_link)
    await db_session.refresh(future_link)
    await db_session.refresh(no_expiry_link)
    
    # Expired link should be soft-deleted
    assert expired_link.deleted_at is not None
    
    # Future and permanent links should not be touched
    assert future_link.deleted_at is None
    assert no_expiry_link.deleted_at is None


@pytest.mark.asyncio
async def test_cleanup_ignores_already_deleted_links(db_session):
    """Test that cleanup job ignores links already soft-deleted."""
    # Create a test user
    user = User(
        email="test@example.com",
        password_hash="dummy",
        api_key_hash="dummy",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    
    # Create an expired link that was already soft-deleted
    expired_time = datetime.now(timezone.utc) - timedelta(hours=2)
    deleted_time = datetime.now(timezone.utc) - timedelta(hours=1)
    already_deleted = Link(
        user_id=user.id,
        slug="already-deleted",
        destination_url="https://example.com/deleted",
        expires_at=expired_time,
        deleted_at=deleted_time,
    )
    
    db_session.add(already_deleted)
    await db_session.commit()
    # Store timestamp without timezone for comparison (SQLite doesn't preserve it)
    original_deleted_at = already_deleted.deleted_at.replace(tzinfo=None) if already_deleted.deleted_at else None
    
    # Run the cleanup job with the test session
    await expire_links_job(session=db_session)
    
    # Refresh and verify deleted_at timestamp hasn't changed
    await db_session.refresh(already_deleted)
    current_deleted_at = already_deleted.deleted_at.replace(tzinfo=None) if already_deleted.deleted_at else None
    assert current_deleted_at == original_deleted_at


@pytest.mark.asyncio
async def test_cleanup_with_no_expired_links(db_session):
    """Test that cleanup runs successfully when there are no expired links."""
    # Create a test user
    user = User(
        email="test@example.com",
        password_hash="dummy",
        api_key_hash="dummy",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    
    # Create only active links
    future_link = Link(
        user_id=user.id,
        slug="active",
        destination_url="https://example.com/active",
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    
    db_session.add(future_link)
    await db_session.commit()
    
    # Run cleanup - should complete without errors
    await expire_links_job(session=db_session)
    
    # Verify link is still active
    await db_session.refresh(future_link)
    assert future_link.deleted_at is None


@pytest.mark.asyncio
async def test_cleanup_multiple_expired_links(db_session):
    """Test cleanup correctly handles multiple expired links at once."""
    # Create a test user
    user = User(
        email="test@example.com",
        password_hash="dummy",
        api_key_hash="dummy",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    
    # Create multiple expired links
    expired_time = datetime.now(timezone.utc) - timedelta(hours=1)
    links = []
    for i in range(5):
        link = Link(
            user_id=user.id,
            slug=f"expired-{i}",
            destination_url=f"https://example.com/expired-{i}",
            expires_at=expired_time,
        )
        links.append(link)
        db_session.add(link)
    
    await db_session.commit()
    
    # Run cleanup with the test session
    await expire_links_job(session=db_session)
    
    # Verify all expired links are soft-deleted
    for link in links:
        await db_session.refresh(link)
        assert link.deleted_at is not None


@pytest.mark.asyncio
async def test_cleanup_preserves_click_counts(db_session):
    """Test that cleanup doesn't affect click counts."""
    # Create a test user
    user = User(
        email="test@example.com",
        password_hash="dummy",
        api_key_hash="dummy",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    
    # Create an expired link with clicks
    expired_time = datetime.now(timezone.utc) - timedelta(hours=1)
    link = Link(
        user_id=user.id,
        slug="expired-with-clicks",
        destination_url="https://example.com/expired",
        expires_at=expired_time,
        click_count=42,
    )
    
    db_session.add(link)
    await db_session.commit()
    
    # Run cleanup with the test session
    await expire_links_job(session=db_session)
    
    # Verify click count is preserved
    await db_session.refresh(link)
    assert link.deleted_at is not None
    assert link.click_count == 42
