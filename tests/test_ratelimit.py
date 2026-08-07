"""
Tests for the API rate-limiting layer (linkvault/ratelimit.py).

Strategy
--------
Each test injects a *fresh* ``RateLimiter`` instance into a dedicated
``RateLimitMiddleware`` so tests are fully isolated from one another and
from the module-level singleton used in production.

The middleware is added to the ``app`` via ``app.add_middleware`` inside a
pytest fixture that tears it down afterwards by rebuilding the middleware
stack — achieved by temporarily replacing ``app.middleware_stack``.

Because Starlette rebuilds the middleware stack lazily on the first request,
we force a rebuild after patching by setting ``app.middleware_stack = None``.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.middleware import Middleware

from linkvault.database import Base, get_db
from linkvault.main import app
from linkvault.ratelimit import RateLimitMiddleware, RateLimiter, _classify, _parse_limit

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


# ---------------------------------------------------------------------------
# Unit tests — pure logic, no HTTP
# ---------------------------------------------------------------------------


class TestParseLimitSpec:
    def test_valid_minute(self) -> None:
        assert _parse_limit("5/minute") == (5, 60)

    def test_valid_second(self) -> None:
        assert _parse_limit("1/second") == (1, 1)

    def test_valid_hour(self) -> None:
        assert _parse_limit("100/hour") == (100, 3600)

    def test_valid_day(self) -> None:
        assert _parse_limit("1000/day") == (1000, 86400)

    def test_case_insensitive_period(self) -> None:
        assert _parse_limit("10/MINUTE") == (10, 60)

    def test_invalid_period_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid rate-limit spec"):
            _parse_limit("10/week")

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_limit("no-slash")

    def test_zero_count_raises(self) -> None:
        with pytest.raises(ValueError, match="must be >= 1"):
            _parse_limit("0/minute")


class TestClassify:
    def test_register_is_auth(self) -> None:
        assert _classify("POST", "/users/register") == "auth"

    def test_token_is_auth(self) -> None:
        assert _classify("POST", "/users/token") == "auth"

    def test_get_users_not_classified(self) -> None:
        # GET /users/* is not a defined route but should not be rate-limited
        # by any specific tier
        assert _classify("GET", "/users/register") is None

    def test_post_links_is_write(self) -> None:
        assert _classify("POST", "/links") == "write"

    def test_patch_links_is_write(self) -> None:
        assert _classify("PATCH", "/links/abc123") == "write"

    def test_delete_links_is_write(self) -> None:
        assert _classify("DELETE", "/links/abc123") == "write"

    def test_get_links_is_read(self) -> None:
        assert _classify("GET", "/links") == "read"

    def test_get_links_slug_is_read(self) -> None:
        assert _classify("GET", "/links/abc123") == "read"

    def test_get_analytics_is_read(self) -> None:
        assert _classify("GET", "/analytics/abc123") == "read"

    def test_get_slug_is_redirect(self) -> None:
        assert _classify("GET", "/abc123") == "redirect"

    def test_health_not_classified(self) -> None:
        assert _classify("GET", "/health") is None

    def test_docs_not_classified(self) -> None:
        assert _classify("GET", "/docs") is None

    def test_openapi_not_classified(self) -> None:
        assert _classify("GET", "/openapi.json") is None


class TestRateLimiterUnit:
    """Async unit tests for the core sliding-window logic."""

    @pytest.mark.asyncio
    async def test_allows_up_to_limit(self) -> None:
        rl = RateLimiter()
        for _ in range(5):
            allowed, remaining, _ = await rl.is_allowed("k", 5, 60)
            assert allowed

    @pytest.mark.asyncio
    async def test_blocks_over_limit(self) -> None:
        rl = RateLimiter()
        for _ in range(5):
            await rl.is_allowed("k", 5, 60)
        allowed, remaining, retry_after = await rl.is_allowed("k", 5, 60)
        assert not allowed
        assert remaining == 0
        assert retry_after >= 1

    @pytest.mark.asyncio
    async def test_remaining_decrements(self) -> None:
        rl = RateLimiter()
        _, r0, _ = await rl.is_allowed("k", 3, 60)
        assert r0 == 2
        _, r1, _ = await rl.is_allowed("k", 3, 60)
        assert r1 == 1
        _, r2, _ = await rl.is_allowed("k", 3, 60)
        assert r2 == 0

    @pytest.mark.asyncio
    async def test_different_keys_are_independent(self) -> None:
        rl = RateLimiter()
        for _ in range(3):
            await rl.is_allowed("a", 3, 60)
        # "a" is now exhausted
        allowed_a, _, _ = await rl.is_allowed("a", 3, 60)
        assert not allowed_a
        # "b" is untouched
        allowed_b, _, _ = await rl.is_allowed("b", 3, 60)
        assert allowed_b

    @pytest.mark.asyncio
    async def test_reset_clears_state(self) -> None:
        rl = RateLimiter()
        for _ in range(3):
            await rl.is_allowed("k", 3, 60)
        rl.reset()
        allowed, _, _ = await rl.is_allowed("k", 3, 60)
        assert allowed


# ---------------------------------------------------------------------------
# Integration fixtures — HTTP-level tests
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def rate_limit_client(db_engine):
    """
    AsyncClient whose app has a *tight* rate-limit middleware injected so
    that tests can trigger 429s with just a handful of requests.

    A fresh ``RateLimiter`` is used so this fixture is fully isolated.
    """
    fresh_limiter = RateLimiter()
    middleware = RateLimitMiddleware(
        app=app,  # unused — middleware is added via add_middleware below
        auth_limit="3/minute",
        write_limit="3/minute",
        read_limit="3/minute",
        redirect_limit="3/minute",
        rate_limiter=fresh_limiter,
    )

    # Starlette stores middleware as a list of Middleware(cls, args, kwargs) objects.
    # We prepend our tight middleware and rebuild the stack.
    original_middleware = list(app.user_middleware)
    app.user_middleware.insert(
        0,
        Middleware(
            RateLimitMiddleware,
            auth_limit="3/minute",
            write_limit="3/minute",
            read_limit="3/minute",
            redirect_limit="3/minute",
            rate_limiter=fresh_limiter,
        ),
    )
    app.middleware_stack = app.build_middleware_stack()

    factory = async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )

    async def override_get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    # Restore original middleware stack
    app.user_middleware = original_middleware
    app.middleware_stack = app.build_middleware_stack()
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Integration tests — HTTP responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_rate_limit_returns_429(rate_limit_client: AsyncClient) -> None:
    """Exceeding the auth tier limit returns 429 with the correct error body."""
    payload = {"email": "rl@example.com", "password": "password123"}
    responses = [
        await rate_limit_client.post("/users/register", json=payload)
        for _ in range(4)
    ]
    status_codes = [r.status_code for r in responses]
    # First 3 should succeed (201 or 409 on duplicate), 4th must be 429
    assert 429 in status_codes, f"Expected a 429 among {status_codes}"
    blocked = next(r for r in responses if r.status_code == 429)
    body = blocked.json()
    assert body["error"] == "rate_limit_exceeded"
    assert "detail" in body


@pytest.mark.asyncio
async def test_429_includes_rate_limit_headers(rate_limit_client: AsyncClient) -> None:
    """A 429 response must carry Retry-After and X-RateLimit-* headers."""
    payload = {"email": "hdr@example.com", "password": "password123"}
    for _ in range(4):
        resp = await rate_limit_client.post("/users/register", json=payload)
        if resp.status_code == 429:
            assert "retry-after" in resp.headers
            assert "x-ratelimit-limit" in resp.headers
            assert "x-ratelimit-remaining" in resp.headers
            assert resp.headers["x-ratelimit-remaining"] == "0"
            return
    pytest.fail("Did not receive a 429 within 4 requests")


@pytest.mark.asyncio
async def test_allowed_responses_include_ratelimit_headers(
    rate_limit_client: AsyncClient,
) -> None:
    """Successful responses must carry X-RateLimit-* informational headers."""
    resp = await rate_limit_client.post(
        "/users/register",
        json={"email": "info@example.com", "password": "password123"},
    )
    assert resp.status_code in (201, 409)
    assert "x-ratelimit-limit" in resp.headers
    assert "x-ratelimit-remaining" in resp.headers


@pytest.mark.asyncio
async def test_read_rate_limit(rate_limit_client: AsyncClient, db_engine) -> None:
    """Authenticated GET /links is subject to the READ tier limit."""
    # Register a user with a separate, unlimited client first
    factory = async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )

    async def override_get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as setup_client:
        reg = await setup_client.post(
            "/users/register",
            json={"email": "readrl@example.com", "password": "password123"},
        )
    api_key = reg.json()["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}

    # Now hammer GET /links with the rate-limited client
    responses = [
        await rate_limit_client.get("/links", headers=headers) for _ in range(4)
    ]
    status_codes = [r.status_code for r in responses]
    assert 429 in status_codes, f"Expected a 429 among {status_codes}"


@pytest.mark.asyncio
async def test_health_endpoint_not_rate_limited(rate_limit_client: AsyncClient) -> None:
    """/health must never be rate-limited regardless of request volume."""
    for _ in range(10):
        resp = await rate_limit_client.get("/health")
        assert resp.status_code == 200, f"Unexpected status {resp.status_code}"


@pytest.mark.asyncio
async def test_different_ips_have_independent_buckets(
    rate_limit_client: AsyncClient,
) -> None:
    """Two different client IPs must not share a rate-limit bucket."""
    # We can't easily change the client IP in ASGI tests, but we can verify
    # that the key derivation for auth tier uses the IP by checking that
    # the limiter key for two different IPs is different.
    from linkvault.ratelimit import _extract_key
    from unittest.mock import MagicMock

    req_a = MagicMock()
    req_a.client.host = "1.2.3.4"
    req_a.headers = {}

    req_b = MagicMock()
    req_b.client.host = "5.6.7.8"
    req_b.headers = {}

    key_a = _extract_key(req_a, "auth")
    key_b = _extract_key(req_b, "auth")
    assert key_a != key_b


@pytest.mark.asyncio
async def test_write_rate_limit_uses_token_key(rate_limit_client: AsyncClient) -> None:
    """Write-tier key is derived from the Bearer token, not the IP."""
    from linkvault.ratelimit import _extract_key
    from unittest.mock import MagicMock

    req = MagicMock()
    req.client.host = "1.2.3.4"
    req.headers = {"authorization": "Bearer my-secret-token"}

    key = _extract_key(req, "write")
    assert "my-secret-token" in key
    assert "1.2.3.4" not in key
