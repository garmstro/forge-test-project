"""
Tests for in-process rate limiting.

Covers:
- SlidingWindowRateLimiter unit behaviour with a fake clock
- GET /{slug} returns 429 + Retry-After once the redirect limit is exceeded
- POST /users/register and POST /users/token return 429 once the API limit is exceeded
- The limit check runs before any DB access
- X-Forwarded-For is not trusted
- RATE_LIMIT_ENABLED=false disables limiting
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient

from linkvault.config import settings
from linkvault.database import get_db
from linkvault.main import app
from linkvault.services.ratelimit import SlidingWindowRateLimiter

REGISTER_URL = "/users/register"
TOKEN_URL = "/users/token"
RATE_LIMITED_BODY = {"error": "rate_limited", "detail": "Too many requests."}


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_allows_n_then_blocks_n_plus_one() -> None:
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(window_seconds=60, clock=clock)
    for i in range(3):
        decision = limiter.hit("1.2.3.4", limit=3)
        assert decision.allowed
        assert decision.remaining == 2 - i
    blocked = limiter.hit("1.2.3.4", limit=3)
    assert not blocked.allowed
    assert blocked.remaining == 0
    assert blocked.retry_after == 60


def test_retry_after_counts_down_and_window_slides() -> None:
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(window_seconds=60, clock=clock)
    assert limiter.hit("k", limit=2).allowed  # t=0
    clock.advance(20)
    assert limiter.hit("k", limit=2).allowed  # t=20
    clock.advance(10)
    blocked = limiter.hit("k", limit=2)  # t=30, oldest hit expires at t=60
    assert not blocked.allowed
    assert blocked.retry_after == 30
    clock.advance(30)  # t=60: first hit leaves the window
    assert limiter.hit("k", limit=2).allowed
    assert not limiter.hit("k", limit=2).allowed  # t=20 hit still inside


def test_window_resets_after_full_window() -> None:
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(window_seconds=60, clock=clock)
    for _ in range(2):
        assert limiter.hit("k", limit=2).allowed
    assert not limiter.hit("k", limit=2).allowed
    clock.advance(60)
    for _ in range(2):
        assert limiter.hit("k", limit=2).allowed
    assert not limiter.hit("k", limit=2).allowed


def test_keys_are_independent() -> None:
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(window_seconds=60, clock=clock)
    assert limiter.hit("a", limit=1).allowed
    assert not limiter.hit("a", limit=1).allowed
    assert limiter.hit("b", limit=1).allowed
    assert not limiter.hit("b", limit=1).allowed


def test_stale_keys_are_pruned_periodically() -> None:
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(
        window_seconds=60, clock=clock, prune_interval_seconds=30
    )
    for i in range(5):
        limiter.hit(f"ip-{i}", limit=10)
    assert len(limiter) == 5
    clock.advance(61)
    limiter.hit("fresh", limit=10)  # triggers the periodic prune
    assert len(limiter) == 1


def test_reset_clears_state() -> None:
    limiter = SlidingWindowRateLimiter(window_seconds=60, clock=FakeClock())
    assert limiter.hit("k", limit=1).allowed
    limiter.reset()
    assert len(limiter) == 0
    assert limiter.hit("k", limit=1).allowed


def test_invalid_arguments_rejected() -> None:
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(window_seconds=0)
    limiter = SlidingWindowRateLimiter()
    with pytest.raises(ValueError):
        limiter.hit("k", limit=0)


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def assert_rate_limited(resp_status: int, body: object, retry_after: str | None) -> None:
    assert resp_status == 429
    assert body == RATE_LIMITED_BODY
    assert retry_after is not None
    assert int(retry_after) >= 1


@pytest.mark.asyncio
async def test_redirect_returns_429_after_limit(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_REDIRECT_PER_MINUTE", 3)
    for _ in range(3):
        resp = await client.get("/no-such-slug")
        assert resp.status_code == 404
        assert resp.json() == {"error": "not_found"}  # body unchanged
    resp = await client.get("/no-such-slug")
    assert_rate_limited(resp.status_code, resp.json(), resp.headers.get("retry-after"))


@pytest.mark.asyncio
async def test_redirect_limit_checked_before_db(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_REDIRECT_PER_MINUTE", 1)
    assert (await client.get("/anything")).status_code == 404

    async def exploding_get_db() -> AsyncIterator[None]:
        raise AssertionError("DB must not be touched when rate limited")
        yield  # pragma: no cover

    app.dependency_overrides[get_db] = exploding_get_db
    resp = await client.get("/anything")
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_x_forwarded_for_is_ignored(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_REDIRECT_PER_MINUTE", 1)
    assert (await client.get("/x", headers={"X-Forwarded-For": "10.0.0.1"})).status_code == 404
    resp = await client.get("/x", headers={"X-Forwarded-For": "10.0.0.2"})
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_register_returns_429_after_limit(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_API_PER_MINUTE", 2)
    for i in range(2):
        resp = await client.post(
            REGISTER_URL, json={"email": f"u{i}@example.com", "password": "password123"}
        )
        assert resp.status_code == 201, resp.text
    resp = await client.post(
        REGISTER_URL, json={"email": "u9@example.com", "password": "password123"}
    )
    assert_rate_limited(resp.status_code, resp.json(), resp.headers.get("retry-after"))


@pytest.mark.asyncio
async def test_token_returns_429_after_limit(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_API_PER_MINUTE", 3)
    creds = {"email": "tok@example.com", "password": "password123"}
    assert (await client.post(REGISTER_URL, json=creds)).status_code == 201
    for _ in range(2):  # register + token share one per-IP budget
        assert (await client.post(TOKEN_URL, json=creds)).status_code == 200
    resp = await client.post(TOKEN_URL, json=creds)
    assert_rate_limited(resp.status_code, resp.json(), resp.headers.get("retry-after"))


@pytest.mark.asyncio
async def test_rate_limit_disabled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(settings, "RATE_LIMIT_REDIRECT_PER_MINUTE", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_API_PER_MINUTE", 1)
    for _ in range(5):
        assert (await client.get("/no-such-slug")).status_code == 404
    creds = {"email": "off@example.com", "password": "password123"}
    assert (await client.post(REGISTER_URL, json=creds)).status_code == 201
    for _ in range(3):
        assert (await client.post(TOKEN_URL, json=creds)).status_code == 200
