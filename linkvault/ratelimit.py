"""
linkvault/ratelimit.py — In-process sliding-window rate limiter.

Architecture
------------
A single ``RateLimiter`` instance is created at module level and shared across
all requests.  It stores per-key hit timestamps in a plain ``dict`` protected
by an ``asyncio.Lock`` so it is safe for concurrent async access within a
single process.

The sliding-window algorithm keeps only the timestamps that fall inside the
current window, so memory usage is bounded by ``max_requests`` per active key.

Rate-limit tiers (configured via ``Settings``)
-----------------------------------------------
* ``RATE_LIMIT_AUTH``      — login / register endpoints (per IP)
* ``RATE_LIMIT_WRITE``     — authenticated write endpoints (per user id)
* ``RATE_LIMIT_READ``      — authenticated read endpoints (per user id)
* ``RATE_LIMIT_REDIRECT``  — public redirect endpoint (per IP)

Each tier is expressed as ``"N/period"`` where *period* is one of
``second``, ``minute``, ``hour``, ``day``.  Examples: ``"5/minute"``,
``"200/hour"``.

Middleware
----------
``RateLimitMiddleware`` is a Starlette ``BaseHTTPMiddleware`` subclass that
intercepts every request, determines the applicable tier and key, and either
lets the request through (adding ``X-RateLimit-*`` headers) or returns a
``429 Too Many Requests`` JSON response with a ``Retry-After`` header.

Route classification
--------------------
The middleware classifies routes by path prefix and HTTP method:

* ``POST /users/register`` or ``POST /users/token``  → AUTH tier, key = client IP
* ``POST|PATCH|DELETE /links*``                       → WRITE tier, key = Bearer token
* ``GET /links*`` or ``GET /analytics*``              → READ tier, key = Bearer token
* ``GET /{slug}`` (everything else that is a GET)     → REDIRECT tier, key = client IP
* Everything else (e.g. ``GET /health``, ``GET /docs``) → not rate-limited
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------

_PERIOD_SECONDS: dict[str, int] = {
    "second": 1,
    "minute": 60,
    "hour": 3_600,
    "day": 86_400,
}


def _parse_limit(spec: str) -> tuple[int, int]:
    """Parse ``"N/period"`` → ``(max_requests, window_seconds)``."""
    try:
        n_str, period = spec.strip().split("/")
        n = int(n_str)
        window = _PERIOD_SECONDS[period.lower()]
    except (ValueError, KeyError) as exc:
        raise ValueError(
            f"Invalid rate-limit spec {spec!r}. "
            "Expected format: '<int>/<second|minute|hour|day>'."
        ) from exc
    if n < 1:
        raise ValueError(f"Rate-limit count must be >= 1, got {n}.")
    return n, window


# ---------------------------------------------------------------------------
# Core sliding-window store
# ---------------------------------------------------------------------------


class RateLimiter:
    """Thread-safe (asyncio) sliding-window rate limiter.

    Each *key* (IP address or user id string) gets its own deque of hit
    timestamps.  On every call to :meth:`is_allowed` the deque is pruned to
    the current window before the decision is made.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # key → deque of monotonic timestamps (float seconds)
        self._hits: dict[str, Deque[float]] = {}

    async def is_allowed(
        self,
        key: str,
        max_requests: int,
        window_seconds: int,
    ) -> tuple[bool, int, int]:
        """Check whether *key* is within its rate limit.

        Returns
        -------
        allowed : bool
            ``True`` if the request should proceed.
        remaining : int
            Number of requests still allowed in the current window.
        retry_after : int
            Seconds until the oldest hit expires (0 when *allowed* is True
            and the window is not yet full).
        """
        now = time.monotonic()
        cutoff = now - window_seconds

        async with self._lock:
            dq: Deque[float] = self._hits.setdefault(key, deque())

            # Evict timestamps outside the window
            while dq and dq[0] <= cutoff:
                dq.popleft()

            count = len(dq)

            if count >= max_requests:
                # Oldest hit tells us when a slot opens up
                retry_after = max(1, int(dq[0] - cutoff))
                return False, 0, retry_after

            # Allow — record this hit
            dq.append(now)
            remaining = max_requests - count - 1
            return True, remaining, 0

    def reset(self) -> None:
        """Clear all stored state (used in tests)."""
        self._hits.clear()


# Module-level singleton shared by the middleware and tests.
limiter = RateLimiter()


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

_WRITE_METHODS = {"POST", "PATCH", "DELETE", "PUT"}


def _classify(method: str, path: str) -> str | None:
    """Return the tier name for this request, or ``None`` if not rate-limited."""
    # Auth endpoints — always per-IP regardless of auth header
    if path in ("/users/register", "/users/token") and method == "POST":
        return "auth"

    # Authenticated write operations on /links
    if path.startswith("/links") and method in _WRITE_METHODS:
        return "write"

    # Authenticated reads on /links and /analytics
    if (path.startswith("/links") or path.startswith("/analytics")) and method == "GET":
        return "read"

    # Public redirect — single-segment path that is not a known prefix
    if (
        method == "GET"
        and "/" not in path.lstrip("/")  # no further slashes → /{slug}
        and path not in ("/health", "/docs", "/redoc", "/openapi.json", "/")
    ):
        return "redirect"

    return None


def _extract_key(request: Request, tier: str) -> str:
    """Derive the rate-limit key for this request + tier combination."""
    if tier in ("auth", "redirect"):
        # Use client IP; fall back to a sentinel so we still rate-limit
        ip = request.client.host if request.client else "unknown"
        return f"{tier}:{ip}"

    # For write/read tiers use the raw Bearer token as the key so that
    # unauthenticated requests (which will be rejected by the auth dep
    # anyway) share a single "no-token" bucket.
    auth_header = request.headers.get("authorization", "")
    token = auth_header.removeprefix("Bearer ").strip() or "no-token"
    return f"{tier}:{token}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that enforces per-tier sliding-window rate limits.

    Parameters
    ----------
    app:
        The ASGI application to wrap.
    auth_limit:
        Rate-limit spec for the AUTH tier (default ``"10/minute"``).
    write_limit:
        Rate-limit spec for the WRITE tier (default ``"60/minute"``).
    read_limit:
        Rate-limit spec for the READ tier (default ``"300/minute"``).
    redirect_limit:
        Rate-limit spec for the REDIRECT tier (default ``"600/minute"``).
    limiter:
        The :class:`RateLimiter` instance to use.  Defaults to the
        module-level singleton so tests can inject their own.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        auth_limit: str = "10/minute",
        write_limit: str = "60/minute",
        read_limit: str = "300/minute",
        redirect_limit: str = "600/minute",
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__(app)
        self._limits: dict[str, tuple[int, int]] = {
            "auth": _parse_limit(auth_limit),
            "write": _parse_limit(write_limit),
            "read": _parse_limit(read_limit),
            "redirect": _parse_limit(redirect_limit),
        }
        self._limiter = rate_limiter if rate_limiter is not None else limiter

    async def dispatch(self, request: Request, call_next: object) -> Response:  # type: ignore[override]
        tier = _classify(request.method, request.url.path)

        if tier is None:
            # Not a rate-limited route — pass through immediately
            return await call_next(request)  # type: ignore[operator]

        max_requests, window_seconds = self._limits[tier]
        key = _extract_key(request, tier)

        allowed, remaining, retry_after = await self._limiter.is_allowed(
            key, max_requests, window_seconds
        )

        if not allowed:
            logger.warning(
                "Rate limit exceeded: tier=%s key=%s retry_after=%ds",
                tier,
                key,
                retry_after,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limit_exceeded",
                    "detail": (
                        f"Too many requests. "
                        f"Please retry after {retry_after} second(s)."
                    ),
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(max_requests),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(retry_after),
                },
            )

        response: Response = await call_next(request)  # type: ignore[operator]

        # Attach informational headers to successful responses
        response.headers["X-RateLimit-Limit"] = str(max_requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = "0"

        return response
