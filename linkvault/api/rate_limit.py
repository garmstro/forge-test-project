"""Rate limiting middleware for API endpoints.

Implements per-IP and per-user rate limiting to prevent abuse.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Callable

from fastapi import Request, status
from fastapi.responses import JSONResponse

# Configuration
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_REQUESTS_PER_IP = 100  # requests per window per IP
RATE_LIMIT_REQUESTS_PER_USER = 50  # requests per window per authenticated user

# In-memory store: {key: [(timestamp, count), ...]}
_rate_limit_store: dict[str, list[tuple[float, int]]] = defaultdict(list)


def _get_client_ip(request: Request) -> str:
    """Extract client IP from request, handling proxies."""
    if request.client:
        return request.client.host
    return "unknown"


def _get_rate_limit_key(request: Request, user_id: str | None = None) -> str:
    """Generate a rate limit key based on user or IP."""
    if user_id:
        return f"user:{user_id}"
    return f"ip:{_get_client_ip(request)}"


def _check_rate_limit(key: str, limit: int) -> bool:
    """Check if a request should be rate limited.
    
    Returns True if the request is allowed, False if it should be rejected.
    """
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    
    # Clean up old entries
    if key in _rate_limit_store:
        _rate_limit_store[key] = [
            (ts, count) for ts, count in _rate_limit_store[key]
            if ts > window_start
        ]
    
    # Count requests in current window
    current_count = sum(count for ts, count in _rate_limit_store[key])
    
    if current_count >= limit:
        return False
    
    # Record this request
    if key not in _rate_limit_store:
        _rate_limit_store[key] = []
    _rate_limit_store[key].append((now, 1))
    
    return True


async def rate_limit_by_ip(request: Request, call_next: Callable) -> JSONResponse:
    """Rate limit middleware by IP address."""
    key = _get_rate_limit_key(request)
    
    if not _check_rate_limit(key, RATE_LIMIT_REQUESTS_PER_IP):
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"error": "rate_limit_exceeded", "detail": "Too many requests from this IP address."},
        )
    
    return await call_next(request)


async def rate_limit_by_user(request: Request, call_next: Callable, user_id: str) -> JSONResponse:
    """Rate limit middleware by authenticated user."""
    key = f"user:{user_id}"
    
    if not _check_rate_limit(key, RATE_LIMIT_REQUESTS_PER_USER):
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"error": "rate_limit_exceeded", "detail": "Too many requests for this user."},
        )
    
    return await call_next(request)
