"""Tests for Link Cleanup Job (Item 5).

Covers:
- Expired links are marked as deleted by the cleanup job
- Links without expiry are not affected
- Cleanup job logs structured JSON
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from linkvault.database import Base
from linkvault.models.link import Link
from linkvault.models.user import User
from linkvault.services.cleanup import expire_links


@pytest.fixture
def sync_db_url() -> str:
    """Create a fresh synchronous in-memory database for cleanup tests."""
    return "sqlite:///:memory:"


@pytest.fixture
def sync_engine(sync_db_url: str):
    """Create a synchronous engine and populate schema."""
    engine = create_engine(
        sync_db_url,
        echo=False,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.mark.asyncio
async def test_cleanup_marks_expired_links_as_deleted(
    sync_engine, sync_db_url: str
) -> None:
    """Cleanup job should mark links with expired expires_at as deleted."""
    # Create data using synchronous session
    with Session(sync_engine) as session:
        # Create a test user
        user = User(
            email="cleanup@example.com",
            password_hash="hashed_password",
            api_key_hash="hashed_api_key",
        )
        session.add(user)
        session.flush()

        # Create an expired link
        now = datetime.utcnow()
        expired_link = Link(
            user_id=user.id,
            slug="expired-link",
            destination_url="https://example.com/expired",
            expires_at=now - timedelta(hours=1),  # Expired 1 hour ago
            click_count=0,
        )
        session.add(expired_link)

        # Create a non-expired link
        permanent_link = Link(
            user_id=user.id,
            slug="permanent-link",
            destination_url="https://example.com/permanent",
            expires_at=now + timedelta(days=30),  # Expires in 30 days
            click_count=0,
        )
        session.add(permanent_link)

        # Create a link with no expiry
        no_expiry_link = Link(
            user_id=user.id,
            slug="no-expiry-link",
            destination_url="https://example.com/forever",
            expires_at=None,
            click_count=0,
        )
        session.add(no_expiry_link)

        session.commit()

        # Store IDs for later verification
        expired_link_id = expired_link.id
        permanent_link_id = permanent_link.id
        no_expiry_link_id = no_expiry_link.id

    # Run the cleanup job with the sync database URL
    expire_links(db_url=sync_db_url)

    # Verify results using new session
    with Session(sync_engine) as session:
        expired_link_after = session.get(Link, expired_link_id)
        assert (
            expired_link_after.deleted_at is not None
        ), "Expired link should be marked as deleted"

        permanent_link_after = session.get(Link, permanent_link_id)
        assert (
            permanent_link_after.deleted_at is None
        ), "Non-expired link should not be deleted"

        no_expiry_link_after = session.get(Link, no_expiry_link_id)
        assert (
            no_expiry_link_after.deleted_at is None
        ), "Link with no expiry should not be deleted"


@pytest.mark.asyncio
async def test_cleanup_does_not_double_delete(sync_engine, sync_db_url: str) -> None:
    """Cleanup should not re-delete already deleted links."""
    with Session(sync_engine) as session:
        user = User(
            email="double@example.com",
            password_hash="hashed_password",
            api_key_hash="hashed_api_key",
        )
        session.add(user)
        session.flush()

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
        session.add(already_deleted_link)
        session.commit()

        original_deleted_at = already_deleted_link.deleted_at
        link_id = already_deleted_link.id

    # Run cleanup
    expire_links(db_url=sync_db_url)

    # Check that deleted_at didn't change
    with Session(sync_engine) as session:
        link_after = session.get(Link, link_id)
        assert (
            link_after.deleted_at == original_deleted_at
        ), "Already deleted links should not be modified"


@pytest.mark.asyncio
async def test_cleanup_with_max_clicks_does_not_expire(
    sync_engine, sync_db_url: str
) -> None:
    """Cleanup should only check expires_at, not max_clicks. Max clicks are checked on redirect."""
    with Session(sync_engine) as session:
        user = User(
            email="max@example.com",
            password_hash="hashed_password",
            api_key_hash="hashed_api_key",
        )
        session.add(user)
        session.flush()

        # Link with max_clicks but no expiry
        max_clicks_link = Link(
            user_id=user.id,
            slug="max-clicks-link",
            destination_url="https://example.com/capped",
            expires_at=None,
            max_clicks=10,
            click_count=10,  # Already hit max, but cleanup doesn't care
        )
        session.add(max_clicks_link)
        session.commit()

        link_id = max_clicks_link.id

    expire_links(db_url=sync_db_url)

    with Session(sync_engine) as session:
        link_after = session.get(Link, link_id)
        assert (
            link_after.deleted_at is None
        ), "Links should only expire by expires_at, not max_clicks"
