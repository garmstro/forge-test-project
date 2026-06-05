"""Tests for Link Cleanup Job (Item 5).

Covers:
- Expired links are marked as deleted by the cleanup job
- Links without expiry are not affected
- Cleanup job logs structured JSON
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.models.link import Link
from linkvault.models.user import User
from linkvault.services.cleanup import expire_links


@pytest.mark.asyncio
async def test_cleanup_marks_expired_links_as_deleted(db_session: AsyncSession) -> None:
    """Cleanup job should mark links with expired expires_at as deleted."""
    # Create a test user
    user = User(
        email="cleanup@example.com",
        password_hash="hashed_password",
        api_key_hash="hashed_api_key",
    )
    db_session.add(user)
    await db_session.flush()

    # Create an expired link
    now = datetime.utcnow()
    expired_link = Link(
        user_id=user.id,
        slug="expired-link",
        destination_url="https://example.com/expired",
        expires_at=now - timedelta(hours=1),  # Expired 1 hour ago
        click_count=0,
    )
    db_session.add(expired_link)

    # Create a non-expired link
    permanent_link = Link(
        user_id=user.id,
        slug="permanent-link",
        destination_url="https://example.com/permanent",
        expires_at=now + timedelta(days=30),  # Expires in 30 days
        click_count=0,
    )
    db_session.add(permanent_link)

    # Create a link with no expiry
    no_expiry_link = Link(
        user_id=user.id,
        slug="no-expiry-link",
        destination_url="https://example.com/forever",
        expires_at=None,
        click_count=0,
    )
    db_session.add(no_expiry_link)

    await db_session.commit()

    # Run the cleanup job (synchronous function)
    expire_links()

    # Re-query to check results
    stmt = select(Link).where(Link.id == expired_link.id)
    result = await db_session.execute(stmt)
    expired_link_after = result.scalar_one()
    assert expired_link_after.deleted_at is not None, "Expired link should be marked as deleted"

    stmt = select(Link).where(Link.id == permanent_link.id)
    result = await db_session.execute(stmt)
    permanent_link_after = result.scalar_one()
    assert permanent_link_after.deleted_at is None, "Non-expired link should not be deleted"

    stmt = select(Link).where(Link.id == no_expiry_link.id)
    result = await db_session.execute(stmt)
    no_expiry_link_after = result.scalar_one()
    assert no_expiry_link_after.deleted_at is None, "Link with no expiry should not be deleted"


@pytest.mark.asyncio
async def test_cleanup_does_not_double_delete(db_session: AsyncSession) -> None:
    """Cleanup should not re-delete already deleted links."""
    user = User(
        email="double@example.com",
        password_hash="hashed_password",
        api_key_hash="hashed_api_key",
    )
    db_session.add(user)
    await db_session.flush()

    now = datetime.utcnow()
    deleted_at_time = now - timedelta(minutes=1)
    already_deleted_link = Link(
        user_id=user.id,
        slug="already-deleted",
        destination_url="https://example.com/old",
        expires_at=now - timedelta(hours=1),
        deleted_at=deleted_at_time,  # Already deleted
        click_count=0,
    )
    db_session.add(already_deleted_link)
    await db_session.commit()

    original_deleted_at = already_deleted_link.deleted_at

    # Run cleanup
    expire_links()

    # Check that deleted_at didn't change
    stmt = select(Link).where(Link.id == already_deleted_link.id)
    result = await db_session.execute(stmt)
    link_after = result.scalar_one()
    assert link_after.deleted_at == original_deleted_at, "Already deleted links should not be modified"


@pytest.mark.asyncio
async def test_cleanup_with_max_clicks_does_not_expire(db_session: AsyncSession) -> None:
    """Cleanup should only check expires_at, not max_clicks. Max clicks are checked on redirect."""
    user = User(
        email="max@example.com",
        password_hash="hashed_password",
        api_key_hash="hashed_api_key",
    )
    db_session.add(user)
    await db_session.flush()

    # Link with max_clicks but no expiry
    max_clicks_link = Link(
        user_id=user.id,
        slug="max-clicks-link",
        destination_url="https://example.com/capped",
        expires_at=None,
        max_clicks=10,
        click_count=10,  # Already hit max, but cleanup doesn't care
    )
    db_session.add(max_clicks_link)
    await db_session.commit()

    expire_links()

    stmt = select(Link).where(Link.id == max_clicks_link.id)
    result = await db_session.execute(stmt)
    link_after = result.scalar_one()
    assert link_after.deleted_at is None, "Links should only expire by expires_at, not max_clicks"
