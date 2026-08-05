"""
Tests for rate limiting functionality.

Covers:
- User-based rate limiting on authenticated endpoints
- IP-based rate limiting on public endpoints
- Rate limit headers in responses
- Rate limit reset behavior
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REGISTER_URL = "/users/register"
TOKEN_URL = "/users/token"
LINKS_URL = "/links"


async def register_user(client: AsyncClient, email: str, password: str):  # type: ignore[return]
    return await client.post(REGISTER_URL, json={"email": email, "password": password})


# ---------------------------------------------------------------------------
# IP-based rate limiting on public endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ip_rate_limit_on_register(client: AsyncClient) -> None:
    """Test that registration endpoint is rate limited by IP."""
    # Make requests up to the limit (default 30)
    for i in range(30):
        resp = await register_user(client, f"user{i}@example.com", "password123")
        assert resp.status_code == 201
        assert "X-RateLimit-Remaining" in resp.headers
        assert "X-RateLimit-Limit" in resp.headers
        assert "X-RateLimit-Reset" in resp.headers

    # The 31st request should be rate limited
    resp = await register_user(client, "user31@example.com", "password123")
    assert resp.status_code == 429
    assert "Rate limit exceeded" in resp.json().get("detail", "")
    assert resp.headers.get("X-RateLimit-Remaining") == "0"


@pytest.mark.asyncio
async def test_ip_rate_limit_on_token(client: AsyncClient) -> None:
    """Test that token endpoint is rate limited by IP."""
    # Register a user first
    await register_user(client, "alice@example.com", "password123")

    # Make token requests up to the limit (default 30)
    for i in range(30):
        resp = await client.post(
            TOKEN_URL, json={"email": "alice@example.com", "password": "password123"}
        )
        assert resp.status_code == 200
        assert "X-RateLimit-Remaining" in resp.headers

    # The 31st request should be rate limited
    resp = await client.post(
        TOKEN_URL, json={"email": "alice@example.com", "password": "password123"}
    )
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_ip_rate_limit_on_redirect(client: AsyncClient) -> None:
    """Test that redirect endpoint is rate limited by IP."""
    # Register and create a link
    reg = await register_user(client, "bob@example.com", "password123")
    api_key = reg.json()["api_key"]

    resp = await client.post(
        LINKS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"url": "https://example.com", "slug": "test-redirect"},
    )
    assert resp.status_code == 201

    # Make redirect requests up to the limit (default 30)
    for i in range(30):
        resp = await client.get("/test-redirect")
        assert resp.status_code in (301, 302)
        assert "X-RateLimit-Remaining" in resp.headers

    # The 31st request should be rate limited
    resp = await client.get("/test-redirect")
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# User-based rate limiting on authenticated endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_rate_limit_on_links_create(client: AsyncClient) -> None:
    """Test that link creation is rate limited per user."""
    # Register a user
    reg = await register_user(client, "charlie@example.com", "password123")
    api_key = reg.json()["api_key"]

    # Make requests up to the limit (default 100)
    for i in range(100):
        resp = await client.post(
            LINKS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"url": f"https://example.com/{i}", "slug": f"link-{i}"},
        )
        assert resp.status_code == 201
        assert "X-RateLimit-Remaining" in resp.headers
        assert "X-RateLimit-Limit" in resp.headers

    # The 101st request should be rate limited
    resp = await client.post(
        LINKS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"url": "https://example.com/over-limit", "slug": "over-limit"},
    )
    assert resp.status_code == 429
    assert "Rate limit exceeded" in resp.json().get("detail", "")


@pytest.mark.asyncio
async def test_user_rate_limit_on_links_list(client: AsyncClient) -> None:
    """Test that link listing is rate limited per user."""
    # Register a user
    reg = await register_user(client, "dave@example.com", "password123")
    api_key = reg.json()["api_key"]

    # Make requests up to the limit (default 100)
    for i in range(100):
        resp = await client.get(
            LINKS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        assert "X-RateLimit-Remaining" in resp.headers

    # The 101st request should be rate limited
    resp = await client.get(
        LINKS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_user_rate_limit_on_analytics(client: AsyncClient) -> None:
    """Test that analytics endpoints are rate limited per user."""
    # Register a user and create a link
    reg = await register_user(client, "eve@example.com", "password123")
    api_key = reg.json()["api_key"]

    resp = await client.post(
        LINKS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"url": "https://example.com", "slug": "analytics-test"},
    )
    assert resp.status_code == 201

    # Make analytics requests up to the limit (default 100)
    for i in range(100):
        resp = await client.get(
            "/analytics/analytics-test",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        assert "X-RateLimit-Remaining" in resp.headers

    # The 101st request should be rate limited
    resp = await client.get(
        "/analytics/analytics-test",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Rate limit headers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_headers_present(client: AsyncClient) -> None:
    """Test that rate limit headers are present in responses."""
    reg = await register_user(client, "frank@example.com", "password123")
    api_key = reg.json()["api_key"]

    resp = await client.get(
        LINKS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    assert "X-RateLimit-Limit" in resp.headers
    assert "X-RateLimit-Remaining" in resp.headers
    assert "X-RateLimit-Reset" in resp.headers

    # Verify header values are integers
    limit = int(resp.headers["X-RateLimit-Limit"])
    remaining = int(resp.headers["X-RateLimit-Remaining"])
    reset = int(resp.headers["X-RateLimit-Reset"])

    assert limit > 0
    assert 0 <= remaining <= limit
    assert reset > 0


@pytest.mark.asyncio
async def test_rate_limit_remaining_decreases(client: AsyncClient) -> None:
    """Test that X-RateLimit-Remaining decreases with each request."""
    reg = await register_user(client, "grace@example.com", "password123")
    api_key = reg.json()["api_key"]

    resp1 = await client.get(
        LINKS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    remaining1 = int(resp1.headers["X-RateLimit-Remaining"])

    resp2 = await client.get(
        LINKS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    remaining2 = int(resp2.headers["X-RateLimit-Remaining"])

    assert remaining2 == remaining1 - 1


# ---------------------------------------------------------------------------
# Different users have independent rate limits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limits_independent_per_user(client: AsyncClient) -> None:
    """Test that different users have independent rate limits."""
    # Register two users
    reg1 = await register_user(client, "henry@example.com", "password123")
    api_key1 = reg1.json()["api_key"]

    reg2 = await register_user(client, "iris@example.com", "password123")
    api_key2 = reg2.json()["api_key"]

    # User 1 makes 50 requests
    for i in range(50):
        resp = await client.get(
            LINKS_URL,
            headers={"Authorization": f"Bearer {api_key1}"},
        )
        assert resp.status_code == 200

    # User 2 should still have full quota
    resp = await client.get(
        LINKS_URL,
        headers={"Authorization": f"Bearer {api_key2}"},
    )
    assert resp.status_code == 200
    remaining = int(resp.headers["X-RateLimit-Remaining"])
    assert remaining == 99  # 100 - 1 for this request
