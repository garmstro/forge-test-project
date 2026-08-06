"""Rate limiting dependencies for FastAPI."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.api.deps import get_current_user
from linkvault.database import get_db
from linkvault.models.user import User
from linkvault.services.rate_limiter import InMemoryRateLimiter, RateLimitConfig

# Global rate limiter instance
_rate_limiter: InMemoryRateLimiter | None = None


def get_rate_limiter(config: RateLimitConfig | None = None) -> InMemoryRateLimiter:
    """Get or create the global rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = InMemoryRateLimiter(config)
    return _rate_limiter


def reset_rate_limiter() -> None:
    """Reset the global rate limiter (useful for testing)."""
    global _rate_limiter
    _rate_limiter = None


async def check_user_rate_limit(
    current_user: User = Depends(get_current_user),
) -> User:
    """Dependency that checks user-based rate limits.

    Raises 429 if the user has exceeded their rate limit.
    """
    limiter = get_rate_limiter()
    is_allowed, stats = limiter.check_user_limit(str(current_user.id))

    if not is_allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please try again later.",
            headers={
                "Retry-After": "60",
                "X-RateLimit-Limit-Minute": str(limiter.config.user_requests_per_minute),
                "X-RateLimit-Limit-Hour": str(limiter.config.user_requests_per_hour),
                "X-RateLimit-Remaining-Minute": str(stats.get("user_minute_remaining", 0)),
                "X-RateLimit-Remaining-Hour": str(stats.get("user_hour_remaining", 0)),
            },
        )

    return current_user


async def check_ip_rate_limit(request: Request) -> str:
    """Dependency that checks IP-based rate limits.

    Raises 429 if the IP address has exceeded its rate limit.
    Returns the IP address.
    """
    ip_address = request.client.host if request.client else "unknown"
    limiter = get_rate_limiter()
    is_allowed, stats = limiter.check_ip_limit(ip_address)

    if not is_allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please try again later.",
            headers={
                "Retry-After": "60",
                "X-RateLimit-Limit-Minute": str(limiter.config.ip_requests_per_minute),
                "X-RateLimit-Limit-Hour": str(limiter.config.ip_requests_per_hour),
                "X-RateLimit-Remaining-Minute": str(stats.get("ip_minute_remaining", 0)),
                "X-RateLimit-Remaining-Hour": str(stats.get("ip_hour_remaining", 0)),
            },
        )

    return ip_address
