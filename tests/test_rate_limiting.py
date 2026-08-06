"""
Tests for rate limiting functionality.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_rate_limit_integration_works(client: AsyncClient):
    """Verify that rate limiting middleware is integrated without breaking the app."""
    # Register a user
    response = await client.post(
        "/users/register",
        json={"email": "ratelimit@example.com", "password": "testpass123"},
    )
    assert response.status_code == 201
    api_key = response.json()["api_key"]
    
    # Make a few requests to verify the app still works
    for _ in range(5):
        response = await client.get(
            "/links",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_endpoint_works(client: AsyncClient):
    """Verify that the health endpoint works correctly."""
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "version" in data


@pytest.mark.asyncio
async def test_multiple_requests_succeed_under_limit(client: AsyncClient):
    """
    Verify that multiple requests succeed when under the rate limit.
    
    Note: In test environment, rate limiting uses in-memory storage
    and may behave differently than in production.
    """
    # Register a user
    register_response = await client.post(
        "/users/register",
        json={"email": "multitest@example.com", "password": "testpass123"},
    )
    assert register_response.status_code == 201
    api_key = register_response.json()["api_key"]
    
    # Make 20 requests - should all succeed
    success_count = 0
    for _ in range(20):
        response = await client.get(
            "/links",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if response.status_code == 200:
            success_count += 1
    
    # All requests should succeed since we're well under the 100/minute limit
    assert success_count == 20


@pytest.mark.asyncio
async def test_different_endpoints_work_with_rate_limiting(client: AsyncClient):
    """Verify rate limiting doesn't break different endpoints."""
    # Register a user
    register_response = await client.post(
        "/users/register",
        json={"email": "endpoints@example.com", "password": "testpass123"},
    )
    assert register_response.status_code == 201
    api_key = register_response.json()["api_key"]
    
    # Test various endpoints
    endpoints_to_test = [
        ("/links", 200),
        ("/analytics/summary", 200),
        ("/health", 200),
    ]
    
    for endpoint, expected_status in endpoints_to_test:
        if endpoint == "/health":
            response = await client.get(endpoint)
        else:
            response = await client.get(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"}
            )
        assert response.status_code == expected_status


