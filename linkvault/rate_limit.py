"""
linkvault/rate_limit.py
=======================
Application-level rate limiting for LinkVault.

Design decisions (see DECISIONS.md §5):
- Uses slowapi (a Starlette/FastAPI wrapper around the `limits` library) with
  an in-memory storage backend.  No external Redis/Memcached dependency.
- Two limit tiers:
    * API tier  — authenticated endpoints (links, analytics, users).
      Key: authenticated user UUID when a Bearer token is present; falls back
      to client IP so that unauthenticated callers (e.g. /users/register) are
      still covered.
    * Redirect tier — the public GET /{slug} hot path.
      Key: client IP only.  The limit is set higher than the API tier so that
      normal browser/bot traffic is not impacted.
- Limits are configurable via RATE_LIMIT_API and RATE_LIMIT_REDIRECT in
  config.py / .env.
- When a limit is exceeded slowapi raises an HTTP 429 with a JSON body:
      {"error": "rate_limit_exceeded", "detail": "..."}
  and a Retry-After header.
- No default limits are set on the middleware; every limit is applied via an
  explicit @limiter.limit(...) or @redirect_limiter.limit(...) decorator so
  that each endpoint's key function and quota are unambiguous.
"""
from __future__ import annotations

import hashlib
import logging

from fastapi import Request
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse

from linkvault.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Key functions
# ---------------------------------------------------------------------------


def _api_key_func(request: Request) -> str:
    """Return a rate-limit key for API endpoints.

    Prefers the authenticated user's ID (derived from the Bearer token hash)
    so that each user has their own independent bucket.  Falls back to the
    client IP address for unauthenticated requests (e.g. /users/register).
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        raw_key = auth_header[7:].strip()
        if raw_key:
            # Use a short hash of the raw key as the bucket identifier.
            # We do NOT look up the DB here — that would add latency.
            # The hash is collision-resistant enough for bucketing purposes.
            return "user:" + hashlib.sha256(raw_key.encode()).hexdigest()[:16]
    # Fall back to IP
    return "ip:" + (get_remote_address(request) or "unknown")


def _redirect_key_func(request: Request) -> str:
    """Return a rate-limit key for the redirect endpoint (IP only)."""
    return "ip:" + (get_remote_address(request) or "unknown")


# ---------------------------------------------------------------------------
# Limiter instances
# ---------------------------------------------------------------------------

# API limiter — used on all authenticated/management endpoints.
# No default_limits: every limit is applied via an explicit decorator.
limiter = Limiter(
    key_func=_api_key_func,
    storage_uri="memory://",
)

# Redirect limiter — separate instance with IP-only key function so that
# the redirect hot path has its own independent counter and quota.
redirect_limiter = Limiter(
    key_func=_redirect_key_func,
    storage_uri="memory://",
)


# ---------------------------------------------------------------------------
# 429 exception handler
# ---------------------------------------------------------------------------


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Return a consistent JSON 429 response when any rate limit is exceeded."""
    logger.warning(
        "Rate limit exceeded: %s %s — %s",
        request.method,
        request.url.path,
        exc.detail,
    )
    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "detail": f"Too many requests. {exc.detail}",
        },
        headers={"Retry-After": "60"},
    )
