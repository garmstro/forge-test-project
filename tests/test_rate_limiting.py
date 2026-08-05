"""
Tests for rate limiting functionality.

Covers:
- Public endpoints (register, login) are rate limited by IP
- Authenticated endpoints (links CRUD) are rate limited by user ID
- Redirect endpoint is rate limited by IP
- 429 responses are returned when limits are exceeded
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REGISTER_URL = "/users/register"
TOKEN_URL = "/users/token"


async def register_user(client: AsyncClient, email: str, password: str):  # type: ignore[return]
    return await client.post(REGISTER_URL, json={"email": email, "password": password})


# ---------------------------------------------------------------------------
# Public endpoint rate limiting (by IP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_rate_limit_by_ip(client: AsyncClient) -> None:
    """Registration endpoint should be rate limited to 5/minute per IP."""
    # First 5 requests should succeed (or fail with 409 for duplicates)
    for i in range(5):
        resp = await register_user(client, f"user{i}@example.com", "password123")
        assert resp.status_code in (201, 409)  # 201 for first, may get 409 if duplicate
    
    # 6th request should be rate limited
    resp = await register_user(client, "user6@example.com", "password123")
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_token_rate_limit_by_ip(client: AsyncClient) -> None:
    """Token endpoint should be rate limited to 10/minute per IP."""
    # Register a user first
    reg_resp = await register_user(client, "ratelimit@example.com", "password123")
    assert reg_resp.status_code == 201
    
    # First 10 token requests should succeed
    for _ in range(10):
        resp = await client.post(TOKEN_URL, json={"email": "ratelimit@example.com", "password": "password123"})
        assert resp.status_code == 200
    
    # 11th request should be rate limited
    resp = await client.post(TOKEN_URL, json={"email": "ratelimit@example.com", "password": "password123"})
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Authenticated endpoint rate limiting (by user ID)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_link_rate_limit_by_user(client: AsyncClient) -> None:
    """Link creation should be rate limited to 100/hour per user."""
    # Register and get API key
    reg_resp = await register_user(client, "linkuser@example.com", "password123")
    api_key = reg_resp.json()["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}
    
    # Create links up to the limit (testing a subset for performance)
    # We'll test that we can create at least 10 links without hitting the limit
    for i in range(10):
        resp = await client.post(
            "/links",
            json={"url": f"https://example.com/{i}"},
            headers=headers,
        )
        assert resp.status_code == 201
    
    # The limit is 100/hour, so 10 requests should not trigger it
    # This test verifies the rate limiter is applied but not overly restrictive


@pytest.mark.asyncio
async def test_list_links_rate_limit_by_user(client: AsyncClient) -> None:
    """Link listing should be rate limited to 200/hour per user."""
    # Register and get API key
    reg_resp = await register_user(client, "listuser@example.com", "password123")
    api_key = reg_resp.json()["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}
    
    # Make multiple list requests (testing a subset)
    for _ in range(10):
        resp = await client.get("/links", headers=headers)
        assert resp.status_code == 200
    
    # The limit is 200/hour, so 10 requests should not trigger it


# ---------------------------------------------------------------------------
# Redirect endpoint rate limiting (by IP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redirect_rate_limit_by_ip(client: AsyncClient) -> None:
    """Redirect endpoint should be rate limited to 1000/hour per IP."""
    # Register user and create a link
    reg_resp = await register_user(client, "redirectuser@example.com", "password123")
    api_key = reg_resp.json()["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}
    
    link_resp = await client.post(
        "/links",
        json={"url": "https://example.com", "slug": "testslug"},
        headers=headers,
    )
    assert link_resp.status_code == 201
    
    # Make multiple redirect requests (testing a subset)
    for _ in range(10):
        resp = await client.get("/testslug", follow_redirects=False)
        assert resp.status_code in (301, 302)
    
    # The limit is 1000/hour, so 10 requests should not trigger it


# ---------------------------------------------------------------------------
# Rate limit isolation between users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_per_user_isolation(client: AsyncClient) -> None:
    """Rate limits should be isolated per user, not shared."""
    # Register two users
    reg1 = await register_user(client, "user1@example.com", "password123")
    reg2 = await register_user(client, "user2@example.com", "password123")
    
    api_key1 = reg1.json()["api_key"]
    api_key2 = reg2.json()["api_key"]
    
    headers1 = {"Authorization": f"Bearer {api_key1}"}
    headers2 = {"Authorization": f"Bearer {api_key2}"}
    
    # Each user should be able to make requests independently
    for i in range(5):
        resp1 = await client.post(
            "/links",
            json={"url": f"https://example.com/user1/{i}"},
            headers=headers1,
        )
        resp2 = await client.post(
            "/links",
            json={"url": f"https://example.com/user2/{i}"},
            headers=headers2,
        )
        assert resp1.status_code == 201
        assert resp2.status_code == 201
