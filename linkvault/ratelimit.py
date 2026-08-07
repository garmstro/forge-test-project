from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from linkvault.config import settings


def get_api_key_or_ip(request: Request) -> str:
    """Extract API key from Authorization header, fall back to IP address.

    Using the API key as the rate-limit key means authenticated users share a
    per-key bucket rather than a per-IP bucket, which is fairer behind proxies
    and more meaningful for abuse prevention.
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]  # raw API key as identifier
    return get_remote_address(request)


# Single shared Limiter instance — imported by routers and main.py.
limiter = Limiter(
    key_func=get_remote_address,
    enabled=settings.RATE_LIMIT_ENABLED,
    default_limits=[settings.RATE_LIMIT_DEFAULT],
)
