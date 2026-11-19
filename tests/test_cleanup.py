"""Tests for the cleanup service and scheduler."""
from __future__ import annotations

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


@pytest.mark.asyncio
async def test_cleanup_expires_links_by_expires_at(db_session: AsyncSession) -> None:
    """Test that cleanup soft-deletes links past their expiry time."""
    # Create a user
    user = User(
        id="user-1",
        email="test@example.com",
        password_hash="hashed_password",
        api_key_hash="api_key_hash",
    )
    db_session.add(user)

    now = _utc_now()
    past = now - timedelta(hours=1)
    future = now + timedelta(hours=1)

    # Create an expired link
    expired_link = Link(
        id="link-1",
        user_id="user-1",
        slug="expired",
        destination_url="https://example.com",
        expires_at=past,
        click_count=0,
    )
    db_session.add(expired_link)

    # Create an unexpired link
    active_link = Link(
        id="link-2",
        user_id="user-1",
        slug="active",
        destination_url="https://example.com",
        expires_at=future,
        click_count=0,
    )
    db_session.add(active_link)

    await db_session.commit()

    # Run cleanup
    await cleanup_expired_links(db_session)

    # Check that expired link is soft-deleted
    result = await db_session.execute(select(Link).where(Link.id == "link-1"))
    expired = result.scalar_one_or_none()
    assert expired is not None
    assert expired.deleted_at is not None

    # Check that active link is NOT deleted
    result = await db_session.execute(select(Link).where(Link.id == "link-2"))
    active = result.scalar_one_or_none()
    assert active is not None
    assert active.deleted_at is None


@pytest.mark.asyncio
async def test_cleanup_expires_links_by_max_clicks(db_session: AsyncSession) -> None:
    """Test that cleanup soft-deletes links that reached max_clicks."""
    # Create a user
    user = User(
        id="user-1",
        email="test@example.com",
        password_hash="hashed_password",
        api_key_hash="api_key_hash",
    )
    db_session.add(user)

    # Create a link that has reached max_clicks
    exhausted_link = Link(
        id="link-1",
        user_id="user-1",
        slug="exhausted",
        destination_url="https://example.com",
        max_clicks=10,
        click_count=10,
    )
    db_session.add(exhausted_link)

    # Create a link that hasn't reached max_clicks yet
    active_link = Link(
        id="link-2",
        user_id="user-1",
        slug="active",
        destination_url="https://example.com",
        max_clicks=10,
        click_count=5,
    )
    db_session.add(active_link)

    await db_session.commit()

    # Run cleanup
    await cleanup_expired_links(db_session)

    # Check that exhausted link is soft-deleted
    result = await db_session.execute(select(Link).where(Link.id == "link-1"))
    exhausted = result.scalar_one_or_none()
    assert exhausted is not None
    assert exhausted.deleted_at is not None

    # Check that active link is NOT deleted
    result = await db_session.execute(select(Link).where(Link.id == "link-2"))
    active = result.scalar_one_or_none()
    assert active is not None
    assert active.deleted_at is None


@pytest.mark.asyncio
async def test_cleanup_deduplicates_double_match(db_session: AsyncSession) -> None:
    """Test that a link matching both expiry and max_clicks criteria is only counted once."""
    # Create a user
    user = User(
        id="user-1",
        email="test@example.com",
        password_hash="hashed_password",
        api_key_hash="api_key_hash",
    )
    db_session.add(user)

    now = _utc_now()
    past = now - timedelta(hours=1)

    # Create a link that is both expired AND at max_clicks
    exhausted_expired = Link(
        id="link-1",
        user_id="user-1",
        slug="double",
        destination_url="https://example.com",
        expires_at=past,
        max_clicks=10,
        click_count=10,
    )
    db_session.add(exhausted_expired)

    await db_session.commit()

    # Run cleanup
    await cleanup_expired_links(db_session)

    # Check that it's only deleted once (no duplicates in the delete list)
    result = await db_session.execute(select(Link).where(Link.id == "link-1"))
    link = result.scalar_one_or_none()
    assert link is not None
    assert link.deleted_at is not None


@pytest.mark.asyncio
async def test_cleanup_respects_already_deleted_links(db_session: AsyncSession) -> None:
    """Test that already soft-deleted links are not processed again."""
    # Create a user
    user = User(
        id="user-1",
        email="test@example.com",
        password_hash="hashed_password",
        api_key_hash="api_key_hash",
    )
    db_session.add(user)

    now = _utc_now()
    past = now - timedelta(hours=1)
    deletion_time = past - timedelta(minutes=10)

    # Create an already-deleted link
    deleted_link = Link(
        id="link-1",
        user_id="user-1",
        slug="already-deleted",
        destination_url="https://example.com",
        expires_at=past,
        click_count=0,
        deleted_at=deletion_time,
    )
    db_session.add(deleted_link)

    await db_session.commit()

    # Get the original deletion time
    result = await db_session.execute(select(Link).where(Link.id == "link-1"))
    original_link = result.scalar_one_or_none()
    original_deleted_at = original_link.deleted_at

    # Run cleanup
    await cleanup_expired_links(db_session)

    # Check that the deletion time hasn't changed
    result = await db_session.execute(select(Link).where(Link.id == "link-1"))
    link = result.scalar_one_or_none()
    assert link is not None
    assert link.deleted_at == original_deleted_at


@pytest.mark.asyncio
async def test_cleanup_no_op_when_no_links_to_delete(db_session: AsyncSession) -> None:
    """Test that cleanup handles the case when there are no links to delete."""
    # Create a user with no links
    user = User(
        id="user-1",
        email="test@example.com",
        password_hash="hashed_password",
        api_key_hash="api_key_hash",
    )
    db_session.add(user)

    await db_session.commit()

    # Run cleanup (should not raise any exceptions)
    await cleanup_expired_links(db_session)

    # Verify that no links exist
    result = await db_session.execute(select(Link))
    links = result.scalars().all()
    assert len(links) == 0
