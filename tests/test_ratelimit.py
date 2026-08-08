"""
Tests for API rate limiting functionality.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_rate_limit_returns_429_when_exceeded(client: AsyncClient) -> None:
    """Verify that exceeding the rate limit returns HTTP 429."""
    # Register a user first
    resp = await client.post(
        "/users/register",
        json={"email": "ratelimit@example.com", "password": "testpass123"},
    )
    assert resp.status_code == 201

    # The auth rate limit is 10/minute. Make 11 requests to exceed it.
    # We'll use the /users/token endpoint which has the auth rate limit.
    for i in range(10):
        resp = await client.post(
            "/users/token",
            json={"email": "ratelimit@example.com", "password": "testpass123"},
        )
        # Should succeed (200) for the first 10 requests
        assert resp.status_code == 200, f"Request {i+1} failed unexpectedly"

    # The 11th request should be rate limited
    resp = await client.post(
        "/users/token",
        json={"email": "ratelimit@example.com", "password": "testpass123"},
    )
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_rate_limit_different_endpoints_have_separate_limits(
    client: AsyncClient,
) -> None:
    """Verify that different endpoint groups have separate rate limit buckets."""
    # Register and get API key
    resp = await client.post(
        "/users/register",
        json={"email": "separate@example.com", "password": "testpass123"},
    )
    assert resp.status_code == 201
    api_key = resp.json()["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}

    # Create a link (uses links rate limit)
    resp = await client.post(
        "/links",
        json={"url": "https://example.com"},
        headers=headers,
    )
    assert resp.status_code == 201
    slug = resp.json()["slug"]

    # Make requests to analytics endpoint (uses analytics rate limit)
    # This should work even if we've made link requests
    resp = await client.get(f"/analytics/{slug}", headers=headers)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_rate_limit_per_api_key_for_authenticated_endpoints(
    client: AsyncClient,
) -> None:
    """Verify that authenticated endpoints rate limit per API key, not per IP."""
    # Register two users
    resp1 = await client.post(
        "/users/register",
        json={"email": "user1@example.com", "password": "testpass123"},
    )
    assert resp1.status_code == 201
    api_key1 = resp1.json()["api_key"]

    resp2 = await client.post(
        "/users/register",
        json={"email": "user2@example.com", "password": "testpass123"},
    )
    assert resp2.status_code == 201
    api_key2 = resp2.json()["api_key"]

    # Make many requests with user1's key
    headers1 = {"Authorization": f"Bearer {api_key1}"}
    for _ in range(30):
        resp = await client.get("/links", headers=headers1)
        assert resp.status_code == 200

    # User2 should still be able to make requests (separate bucket)
    headers2 = {"Authorization": f"Bearer {api_key2}"}
    resp = await client.get("/links", headers=headers2)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_rate_limit_response_format(client: AsyncClient) -> None:
    """Verify that rate limit exceeded response has the expected format."""
    # Register a user
    resp = await client.post(
        "/users/register",
        json={"email": "format@example.com", "password": "testpass123"},
    )
    assert resp.status_code == 201

    # Exceed the rate limit
    for _ in range(10):
        await client.post(
            "/users/token",
            json={"email": "format@example.com", "password": "testpass123"},
        )

    # Check the 429 response format
    resp = await client.post(
        "/users/token",
        json={"email": "format@example.com", "password": "testpass123"},
    )
    assert resp.status_code == 429
    data = resp.json()
    assert "error" in data
    assert "Rate limit exceeded" in data["error"]
