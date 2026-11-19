from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, status

from linkvault.config import settings
from linkvault.services.rate_limiter import limiter


def rate_limit(scope: str, limit_getter: Callable[[], int]) -> Callable[[Request], Awaitable[None]]:
    """Build a FastAPI dependency limiting requests per client IP for *scope*.

    *limit_getter* is called on every request so the limit is read from
    settings at call time.
    """

    async def dependency(request: Request) -> None:
        if not settings.RATE_LIMIT_ENABLED:
            return
        client_key = request.client.host if request.client else "unknown"
        allowed, retry_after = limiter.check(f"{scope}:{client_key}", limit_getter())
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Try again later.",
                headers={"Retry-After": str(retry_after)},
            )

    return dependency
