from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.config import settings
from linkvault.models.rate_limit import RateLimitStore

logger = logging.getLogger(__name__)


class RateLimitService:
    """Service for checking and enforcing rate limits.
    
    Supports two types of rate limiting:
    1. Per-user rate limiting (for authenticated requests)
    2. Per-IP rate limiting (for public/unauthenticated requests)
    """

    @staticmethod
    async def check_user_rate_limit(
        user_id: str,
        db: AsyncSession,
    ) -> tuple[bool, dict[str, int]]:
        """Check if a user has exceeded their rate limit.
        
        Returns:
            (is_allowed, info_dict) where:
            - is_allowed: True if the request should be allowed
            - info_dict: Contains 'remaining', 'limit', 'reset_at' for response headers
        """
        if not settings.RATE_LIMITING_ENABLED:
            return True, {
                "remaining": settings.RATE_LIMIT_AUTHENTICATED_REQUESTS,
                "limit": settings.RATE_LIMIT_AUTHENTICATED_REQUESTS,
                "reset_at": int((datetime.utcnow() + timedelta(seconds=settings.RATE_LIMIT_AUTHENTICATED_WINDOW_SECONDS)).timestamp()),
            }

        now = datetime.utcnow()
        window_start = now - timedelta(seconds=settings.RATE_LIMIT_AUTHENTICATED_WINDOW_SECONDS)

        # Find or create the rate limit entry for this user
        result = await db.execute(
            select(RateLimitStore).where(
                RateLimitStore.user_id == user_id,
                RateLimitStore.window_start > window_start,
            )
        )
        entry = result.scalar_one_or_none()

        if entry is None:
            # Create a new entry for this window
            entry = RateLimitStore(
                user_id=user_id,
                request_count=1,
                window_start=now,
            )
            db.add(entry)
            await db.flush()
            
            reset_at = int((now + timedelta(seconds=settings.RATE_LIMIT_AUTHENTICATED_WINDOW_SECONDS)).timestamp())
            return True, {
                "remaining": settings.RATE_LIMIT_AUTHENTICATED_REQUESTS - 1,
                "limit": settings.RATE_LIMIT_AUTHENTICATED_REQUESTS,
                "reset_at": reset_at,
            }

        # Check if we've exceeded the limit
        is_allowed = entry.request_count < settings.RATE_LIMIT_AUTHENTICATED_REQUESTS
        
        # Increment the counter
        entry.request_count += 1
        db.add(entry)
        await db.flush()

        reset_at = int((entry.window_start + timedelta(seconds=settings.RATE_LIMIT_AUTHENTICATED_WINDOW_SECONDS)).timestamp())
        remaining = max(0, settings.RATE_LIMIT_AUTHENTICATED_REQUESTS - entry.request_count)
        
        return is_allowed, {
            "remaining": remaining,
            "limit": settings.RATE_LIMIT_AUTHENTICATED_REQUESTS,
            "reset_at": reset_at,
        }

    @staticmethod
    async def check_ip_rate_limit(
        ip_address: str,
        db: AsyncSession,
    ) -> tuple[bool, dict[str, int]]:
        """Check if an IP address has exceeded their rate limit.
        
        Returns:
            (is_allowed, info_dict) where:
            - is_allowed: True if the request should be allowed
            - info_dict: Contains 'remaining', 'limit', 'reset_at' for response headers
        """
        if not settings.RATE_LIMITING_ENABLED:
            return True, {
                "remaining": settings.RATE_LIMIT_IP_REQUESTS,
                "limit": settings.RATE_LIMIT_IP_REQUESTS,
                "reset_at": int((datetime.utcnow() + timedelta(seconds=settings.RATE_LIMIT_IP_WINDOW_SECONDS)).timestamp()),
            }

        now = datetime.utcnow()
        window_start = now - timedelta(seconds=settings.RATE_LIMIT_IP_WINDOW_SECONDS)

        # Find or create the rate limit entry for this IP
        result = await db.execute(
            select(RateLimitStore).where(
                RateLimitStore.ip_address == ip_address,
                RateLimitStore.window_start > window_start,
            )
        )
        entry = result.scalar_one_or_none()

        if entry is None:
            # Create a new entry for this window
            entry = RateLimitStore(
                ip_address=ip_address,
                request_count=1,
                window_start=now,
            )
            db.add(entry)
            await db.flush()
            
            reset_at = int((now + timedelta(seconds=settings.RATE_LIMIT_IP_WINDOW_SECONDS)).timestamp())
            return True, {
                "remaining": settings.RATE_LIMIT_IP_REQUESTS - 1,
                "limit": settings.RATE_LIMIT_IP_REQUESTS,
                "reset_at": reset_at,
            }

        # Check if we've exceeded the limit
        is_allowed = entry.request_count < settings.RATE_LIMIT_IP_REQUESTS
        
        # Increment the counter
        entry.request_count += 1
        db.add(entry)
        await db.flush()

        reset_at = int((entry.window_start + timedelta(seconds=settings.RATE_LIMIT_IP_WINDOW_SECONDS)).timestamp())
        remaining = max(0, settings.RATE_LIMIT_IP_REQUESTS - entry.request_count)
        
        return is_allowed, {
            "remaining": remaining,
            "limit": settings.RATE_LIMIT_IP_REQUESTS,
            "reset_at": reset_at,
        }

    @staticmethod
    async def cleanup_expired_entries(db: AsyncSession) -> int:
        """Delete rate limit entries older than the maximum window duration.
        
        Returns:
            Number of entries deleted
        """
        max_window = max(
            settings.RATE_LIMIT_AUTHENTICATED_WINDOW_SECONDS,
            settings.RATE_LIMIT_IP_WINDOW_SECONDS,
        )
        cutoff = datetime.utcnow() - timedelta(seconds=max_window * 2)  # 2x for safety margin
        
        result = await db.execute(
            delete(RateLimitStore).where(RateLimitStore.created_at < cutoff)
        )
        await db.flush()
        
        deleted = result.rowcount
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} expired rate limit entries")
        
        return deleted
