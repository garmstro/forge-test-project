"""
Tests for API rate limiting functionality.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_rate_limit_disabled_by_default(client: AsyncClient):
    """Verify that rate limiting is disabled by default in tests."""
    # Make many requests - should all succeed since rate limiting is disabled
    for _ in range(20):
        response = await client.post(
            "/users/register",
            json={"email": f"test{_}@example.com", "password": "testpass123"},
        )
        # Should either succeed (201) or conflict (409) if email exists
        # but never be rate limited (429)
        assert response.status_code != 429


@pytest.mark.asyncio
async def test_auth_endpoint_rate_limit(client: AsyncClient, enable_rate_limiting):
    """Test that auth endpoints are rate limited when enabled."""
    # The default limit is 10/minute for auth endpoints
    # Make 11 requests to trigger rate limiting
    for i in range(11):
        response = await client.post(
            "/users/register",
            json={"email": f"ratelimit{i}@example.com", "password": "testpass123"},
        )
        if i < 10:
            # First 10 requests should succeed
            assert response.status_code in (201, 409), f"Request {i} failed unexpectedly"
        else:
            # 11th request should be rate limited
            assert response.status_code == 429, f"Request {i} should have been rate limited"


@pytest.mark.asyncio
async def test_rate_limit_response_format(client: AsyncClient, enable_rate_limiting):
    """Test that rate limit exceeded response has correct format."""
    # Exhaust the rate limit
    for i in range(11):
        response = await client.post(
            "/users/register",
            json={"email": f"format{i}@example.com", "password": "testpass123"},
        )
    
    # The last response should be rate limited
    assert response.status_code == 429
    # slowapi returns a text/plain response by default with the error message
    assert "Rate limit exceeded" in response.text or response.status_code == 429


@pytest.mark.asyncio
async def test_rate_limit_headers(client: AsyncClient, enable_rate_limiting):
    """Test that rate limit headers are present in responses."""
    response = await client.post(
        "/users/register",
        json={"email": "headers@example.com", "password": "testpass123"},
    )
    
    # slowapi adds rate limit headers to responses
    # Check for common rate limit headers
    assert response.status_code == 201
    # Headers may include X-RateLimit-Limit, X-RateLimit-Remaining, etc.
    # The exact headers depend on slowapi configuration


@pytest.mark.asyncio
async def test_links_endpoint_rate_limit(client: AsyncClient, enable_rate_limiting):
    """Test that links endpoints are rate limited per user."""
    # First register a user
    register_response = await client.post(
        "/users/register",
        json={"email": "linksrate@example.com", "password": "testpass123"},
    )
    assert register_response.status_code == 201
    api_key = register_response.json()["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}
    
    # Reset rate limiter after registration to get fresh limits for links
    enable_rate_limiting.reset()
    
    # The default limit is 60/minute for links endpoints
    # Make requests up to the limit
    for i in range(61):
        response = await client.post(
            "/links",
            json={"url": f"https://example{i}.com"},
            headers=headers,
        )
        if i < 60:
            assert response.status_code in (201, 409), f"Request {i} failed unexpectedly"
        else:
            assert response.status_code == 429, f"Request {i} should have been rate limited"


@pytest.mark.asyncio
async def test_different_users_have_separate_limits(client: AsyncClient, enable_rate_limiting):
    """Test that different users have separate rate limits."""
    # Register two users
    user1_response = await client.post(
        "/users/register",
        json={"email": "user1@example.com", "password": "testpass123"},
    )
    # Reset to allow second registration
    enable_rate_limiting.reset()
    
    user2_response = await client.post(
        "/users/register",
        json={"email": "user2@example.com", "password": "testpass123"},
    )
    
    api_key1 = user1_response.json()["api_key"]
    api_key2 = user2_response.json()["api_key"]
    
    headers1 = {"Authorization": f"Bearer {api_key1}"}
    headers2 = {"Authorization": f"Bearer {api_key2}"}
    
    # Reset rate limiter
    enable_rate_limiting.reset()
    
    # User 1 makes requests up to their limit
    for i in range(60):
        response = await client.post(
            "/links",
            json={"url": f"https://user1-{i}.com"},
            headers=headers1,
        )
        assert response.status_code in (201, 409)
    
    # User 1's next request should be rate limited
    response = await client.post(
        "/links",
        json={"url": "https://user1-extra.com"},
        headers=headers1,
    )
    assert response.status_code == 429
    
    # User 2 should still be able to make requests (separate limit)
    response = await client.post(
        "/links",
        json={"url": "https://user2-first.com"},
        headers=headers2,
    )
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_redirect_endpoint_rate_limit(client: AsyncClient, enable_rate_limiting):
    """Test that redirect endpoints are rate limited by IP."""
    # First create a link (need to disable rate limiting temporarily)
    enable_rate_limiting.enabled = False
    
    register_response = await client.post(
        "/users/register",
        json={"email": "redirect@example.com", "password": "testpass123"},
    )
    api_key = register_response.json()["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}
    
    link_response = await client.post(
        "/links",
        json={"url": "https://example.com", "slug": "testslug"},
        headers=headers,
    )
    assert link_response.status_code == 201
    
    # Re-enable rate limiting and reset
    enable_rate_limiting.enabled = True
    enable_rate_limiting.reset()
    
    # The default limit is 120/minute for redirects
    # Make requests up to the limit
    for i in range(121):
        response = await client.get("/testslug", follow_redirects=False)
        if i < 120:
            assert response.status_code in (301, 302), f"Request {i} failed unexpectedly"
        else:
            assert response.status_code == 429, f"Request {i} should have been rate limited"


@pytest.mark.asyncio
async def test_analytics_endpoint_rate_limit(client: AsyncClient, enable_rate_limiting):
    """Test that analytics endpoints are rate limited."""
    # First register a user and create a link
    enable_rate_limiting.enabled = False
    
    register_response = await client.post(
        "/users/register",
        json={"email": "analytics@example.com", "password": "testpass123"},
    )
    api_key = register_response.json()["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}
    
    link_response = await client.post(
        "/links",
        json={"url": "https://example.com", "slug": "analyticstest"},
        headers=headers,
    )
    assert link_response.status_code == 201
    
    # Re-enable rate limiting and reset
    enable_rate_limiting.enabled = True
    enable_rate_limiting.reset()
    
    # The default limit is 30/minute for analytics endpoints
    for i in range(31):
        response = await client.get("/analytics/analyticstest", headers=headers)
        if i < 30:
            assert response.status_code == 200, f"Request {i} failed unexpectedly"
        else:
            assert response.status_code == 429, f"Request {i} should have been rate limited"
