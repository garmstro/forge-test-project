"""Tests for the link cleanup service and scheduler."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.models.link import Link
from linkvault.models.user import User
from linkvault.services.cleanup import cleanup_expired_links


class TestCleanupService:
    """Test the cleanup_expired_links() service function."""

    @pytest.mark.asyncio
    async def test_cleanup_no_expired_links(self, db_session: AsyncSession) -> None:
        """Test cleanup when there are no expired links."""
        # Create a user
        user = User(email="test@example.com", password_hash="hashed_pw", api_key_hash="key_hash")
        db_session.add(user)
        await db_session.flush()

        # Create a link that expires in the future
        future_date = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
        link = Link(
            user_id=user.id,
            slug="future-link",
            destination_url="https://example.com",
            expires_at=future_date,
        )
        db_session.add(link)
        await db_session.commit()

        # Run cleanup
        with patch("linkvault.services.cleanup.logger") as mock_logger:
            await cleanup_expired_links(db_session)

            # Verify the link was not deleted
            result = await db_session.execute(select(Link).where(Link.slug == "future-link"))
            link_after = result.scalar_one()
            assert link_after.deleted_at is None

            # Verify logger was called with count = 0
            mock_logger.info.assert_called_once()
            log_data = json.loads(mock_logger.info.call_args[0][0])
            assert log_data["event"] == "cleanup"
            assert log_data["links_expired"] == 0

    @pytest.mark.asyncio
    async def test_cleanup_with_expired_link(self, db_session: AsyncSession) -> None:
        """Test cleanup marks an expired link with deleted_at."""
        # Create a user
        user = User(email="test@example.com", password_hash="hashed_pw", api_key_hash="key_hash")
        db_session.add(user)
        await db_session.flush()

        # Create a link that expired in the past
        past_date = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        link = Link(
            user_id=user.id,
            slug="expired-link",
            destination_url="https://example.com",
            expires_at=past_date,
        )
        db_session.add(link)
        await db_session.commit()

        # Run cleanup
        with patch("linkvault.services.cleanup.logger") as mock_logger:
            await cleanup_expired_links(db_session)

            # Verify the link was deleted (soft-delete)
            result = await db_session.execute(select(Link).where(Link.slug == "expired-link"))
            link_after = result.scalar_one()
            assert link_after.deleted_at is not None

            # Verify logger was called with count = 1
            mock_logger.info.assert_called_once()
            log_data = json.loads(mock_logger.info.call_args[0][0])
            assert log_data["event"] == "cleanup"
            assert log_data["links_expired"] == 1

    @pytest.mark.asyncio
    async def test_cleanup_skips_non_expired_links(self, db_session: AsyncSession) -> None:
        """Test that non-expired links are not affected."""
        user = User(email="test@example.com", password_hash="hashed_pw", api_key_hash="key_hash")
        db_session.add(user)
        await db_session.flush()

        # Create a link with no expiry
        link1 = Link(
            user_id=user.id,
            slug="no-expiry",
            destination_url="https://example.com",
            expires_at=None,
        )
        db_session.add(link1)

        # Create a link that expires in the future
        future_date = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
        link2 = Link(
            user_id=user.id,
            slug="future-expiry",
            destination_url="https://example.com",
            expires_at=future_date,
        )
        db_session.add(link2)
        await db_session.commit()

        # Run cleanup
        await cleanup_expired_links(db_session)

        # Both links should still be active (not deleted)
        result1 = await db_session.execute(select(Link).where(Link.slug == "no-expiry"))
        result2 = await db_session.execute(select(Link).where(Link.slug == "future-expiry"))
        assert result1.scalar_one().deleted_at is None
        assert result2.scalar_one().deleted_at is None

    @pytest.mark.asyncio
    async def test_cleanup_skips_already_deleted_links(self, db_session: AsyncSession) -> None:
        """Test that already-deleted links are skipped."""
        user = User(email="test@example.com", password_hash="hashed_pw", api_key_hash="key_hash")
        db_session.add(user)
        await db_session.flush()

        # Create a link that's already soft-deleted and expired
        past_date = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        deleted_date = datetime.now(timezone.utc).replace(tzinfo=None)
        link = Link(
            user_id=user.id,
            slug="already-deleted",
            destination_url="https://example.com",
            expires_at=past_date,
            deleted_at=deleted_date,
        )
        db_session.add(link)
        await db_session.commit()

        # Run cleanup
        with patch("linkvault.services.cleanup.logger") as mock_logger:
            await cleanup_expired_links(db_session)

            # Verify logger was called with count = 0 (no new deletions)
            mock_logger.info.assert_called_once()
            log_data = json.loads(mock_logger.info.call_args[0][0])
            assert log_data["links_expired"] == 0

    @pytest.mark.asyncio
    async def test_cleanup_multiple_expired_links(self, db_session: AsyncSession) -> None:
        """Test cleanup with multiple expired links."""
        user = User(email="test@example.com", password_hash="hashed_pw", api_key_hash="key_hash")
        db_session.add(user)
        await db_session.flush()

        past_date = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)

        # Create 5 expired links
        for i in range(5):
            link = Link(
                user_id=user.id,
                slug=f"expired-{i}",
                destination_url="https://example.com",
                expires_at=past_date,
            )
            db_session.add(link)

        await db_session.commit()

        # Run cleanup
        with patch("linkvault.services.cleanup.logger") as mock_logger:
            await cleanup_expired_links(db_session)

            # Verify logger was called with count = 5
            mock_logger.info.assert_called_once()
            log_data = json.loads(mock_logger.info.call_args[0][0])
            assert log_data["links_expired"] == 5

            # Verify all 5 links were deleted
            for i in range(5):
                result = await db_session.execute(select(Link).where(Link.slug == f"expired-{i}"))
                link = result.scalar_one()
                assert link.deleted_at is not None


class TestSchedulerLifecycle:
    """Test scheduler lifecycle (integration with async context)."""

    @pytest.mark.asyncio
    async def test_cleanup_job_integration(self, db_session: AsyncSession) -> None:
        """Test that the cleanup job can be called in an async context."""
        # This is a simplified test to verify the job can run in async context
        user = User(email="test@example.com", password_hash="hashed_pw", api_key_hash="key_hash")
        db_session.add(user)
        await db_session.flush()

        past_date = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        link = Link(
            user_id=user.id,
            slug="expired-test",
            destination_url="https://example.com",
            expires_at=past_date,
        )
        db_session.add(link)
        await db_session.commit()

        # Run cleanup directly (as the scheduler would)
        await cleanup_expired_links(db_session)

        # Verify the link was marked deleted
        result = await db_session.execute(select(Link).where(Link.slug == "expired-test"))
        link_after = result.scalar_one()
        assert link_after.deleted_at is not None
