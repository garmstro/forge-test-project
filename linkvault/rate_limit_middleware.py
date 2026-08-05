"""
Rate limiting middleware and dependencies for FastAPI.

Provides middleware for IP-based rate limiting and dependencies for user-based rate limiting.
"""
from __future__ import annotations

from typing import Callable, Optional

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from linkvault.rate_limiter import RateLimitConfig, get_rate_limiter


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Middleware that applies per-IP rate limiting to all requests.

    Adds rate limit headers to responses and returns 429 if limit exceeded.
    """

    def __init__(
        self,
        app,
        requests_per_minute: int = 60,
        requests_per_hour: int = 1000,
        excluded_paths: Optional[list[str]] = None,
    ):
        """
        Initialize the rate limit middleware.

        Args:
            app: The FastAPI application
            requests_per_minute: Max requests per minute per IP
            requests_per_hour: Max requests per hour per IP
            excluded_paths: List of paths to exclude from rate limiting (e.g., ["/health"])
        """
        super().__init__(app)
        self.config = RateLimitConfig(
            requests_per_minute=requests_per_minute,
            requests_per_hour=requests_per_hour,
        )
        self.excluded_paths = excluded_paths or []
        self.rate_limiter = get_rate_limiter()

    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from request, handling X-Forwarded-For header."""
        # Check for X-Forwarded-For header (proxy/load balancer)
        if "x-forwarded-for" in request.headers:
            # Take the first IP in the chain
            return request.headers["x-forwarded-for"].split(",")[0].strip()
        # Fall back to direct connection IP
        return request.client.host if request.client else "unknown"

    def _should_exclude(self, path: str) -> bool:
        """Check if path should be excluded from rate limiting."""
        for excluded in self.excluded_paths:
            if path.startswith(excluded):
                return True
        return False

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request through rate limiter."""
        path = request.url.path

        # Skip rate limiting for excluded paths
        if self._should_exclude(path):
            return await call_next(request)

        client_ip = self._get_client_ip(request)
        is_allowed, limit_info = self.rate_limiter.is_allowed(client_ip, self.config)

        # Add rate limit headers to response
        response = await call_next(request)
        response.headers["X-RateLimit-Limit-Per-Minute"] = str(
            limit_info["limit_per_minute"]
        )
        response.headers["X-RateLimit-Remaining-Per-Minute"] = str(
            limit_info["remaining_per_minute"]
        )
        response.headers["X-RateLimit-Reset-Minute"] = str(limit_info["reset_minute_at"])
        response.headers["X-RateLimit-Limit-Per-Hour"] = str(
            limit_info["limit_per_hour"]
        )
        response.headers["X-RateLimit-Remaining-Per-Hour"] = str(
            limit_info["remaining_per_hour"]
        )
        response.headers["X-RateLimit-Reset-Hour"] = str(limit_info["reset_hour_at"])

        if not is_allowed:
            # Return 429 Too Many Requests
            response.status_code = status.HTTP_429_TOO_MANY_REQUESTS
            response.headers["Retry-After"] = str(
                limit_info["reset_minute_at"] - int(__import__("time").time())
            )

        return response


def create_user_rate_limiter(
    requests_per_minute: int = 30,
    requests_per_hour: int = 500,
) -> Callable:
    """
    Create a dependency for per-user rate limiting.

    This is meant to be used with get_current_user to rate limit authenticated endpoints.

    Args:
        requests_per_minute: Max requests per minute per user
        requests_per_hour: Max requests per hour per user

    Returns:
        A FastAPI dependency function
    """
    config = RateLimitConfig(
        requests_per_minute=requests_per_minute,
        requests_per_hour=requests_per_hour,
    )
    rate_limiter = get_rate_limiter()

    async def check_user_rate_limit(user_id: str) -> dict[str, int]:
        """
        Check if user is within rate limits.

        Raises HTTPException with 429 if limit exceeded.
        Returns limit info dict.
        """
        is_allowed, limit_info = rate_limiter.is_allowed(f"user:{user_id}", config)

        if not is_allowed:
            import time

            retry_after = limit_info["reset_minute_at"] - int(time.time())
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Too many requests.",
                headers={"Retry-After": str(max(1, retry_after))},
            )

        return limit_info

    return check_user_rate_limit

