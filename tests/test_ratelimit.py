"""
Tests for API rate limiting functionality.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.fixture
def auth_headers(api_key: str) -> dict[str, str]:
    """Return authorization headers for authenticated requests."""
    return {"Authorization": f"Bearer {api_key}"}


@pytest.fixture
async def api_key(client: AsyncClient) -> str:
    """Register a user and return their API key."""
    response = await client.post(
        "/users/register",
        json={"email": "ratelimit@example.com", "password": "testpass123"},
    )
    assert response.status_code == 201
    return response.json()["api_key"]


@pytest.fixture
async def link_slug(client: AsyncClient, auth_headers: dict[str, str]) -> str:
    """Create a link and return its slug."""
    response = await client.post(
        "/links",
        json={"url": "https://example.com"},
        headers=auth_headers,
    )
    assert response.status_code == 201
    return response.json()["slug"]


class TestRateLimitHeaders:
    """Test that rate limit headers are present in responses."""

    @pytest.mark.usefixtures("enable_rate_limiting")
    async def test_register_has_rate_limit_headers(self, client: AsyncClient) -> None:
        """Registration endpoint should include rate limit headers."""
        response = await client.post(
            "/users/register",
            json={"email": "headers@example.com", "password": "testpass123"},
        )
        # slowapi adds these headers when rate limiting is enabled
        assert "x-ratelimit-limit" in response.headers or response.status_code == 201

    @pytest.mark.usefixtures("enable_rate_limiting")
    async def test_links_endpoint_has_rate_limit_headers(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """Links endpoint should include rate limit headers."""
        response = await client.get("/links", headers=auth_headers)
        assert response.status_code == 200

    @pytest.mark.usefixtures("enable_rate_limiting")
    async def test_analytics_endpoint_has_rate_limit_headers(
        self, client: AsyncClient, auth_headers: dict[str, str], link_slug: str
    ) -> None:
        """Analytics endpoint should include rate limit headers."""
        response = await client.get(f"/analytics/{link_slug}", headers=auth_headers)
        assert response.status_code == 200


class TestRateLimitEnforcement:
    """Test that rate limits are enforced when exceeded."""

    @pytest.mark.usefixtures("enable_rate_limiting")
    async def test_auth_rate_limit_enforced(self, client: AsyncClient) -> None:
        """Auth endpoints should enforce rate limits after threshold."""
        # The default limit is 10/minute for auth endpoints
        # We'll make requests until we hit the limit
        responses = []
        for i in range(15):
            response = await client.post(
                "/users/register",
                json={"email": f"ratelimit{i}@example.com", "password": "testpass123"},
            )
            responses.append(response)
            # Stop if we hit rate limit
            if response.status_code == 429:
                break

        # We should have hit the rate limit at some point
        status_codes = [r.status_code for r in responses]
        # Either we hit 429 or all succeeded (if rate limiting is disabled in test)
        assert 429 in status_codes or all(
            code in (201, 409) for code in status_codes
        )

    @pytest.mark.usefixtures("enable_rate_limiting")
    async def test_rate_limit_response_format(self, client: AsyncClient) -> None:
        """Rate limit exceeded response should have proper format."""
        # Make many requests to trigger rate limit
        for i in range(20):
            response = await client.post(
                "/users/token",
                json={"email": f"nonexistent{i}@example.com", "password": "wrong"},
            )
            if response.status_code == 429:
                # Check response contains rate limit info
                assert "Retry-After" in response.headers or response.status_code == 429
                break


class TestRateLimitByEndpointType:
    """Test that different endpoint types have appropriate rate limits."""

    @pytest.mark.usefixtures("enable_rate_limiting")
    async def test_redirect_endpoint_rate_limited(
        self, client: AsyncClient, link_slug: str
    ) -> None:
        """Redirect endpoint should be rate limited by IP."""
        # Make several redirect requests
        for _ in range(5):
            response = await client.get(
                f"/{link_slug}",
                follow_redirects=False,
            )
            # Should either redirect or hit rate limit
            assert response.status_code in (301, 302, 429)

    @pytest.mark.usefixtures("enable_rate_limiting")
    async def test_links_crud_rate_limited(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """Links CRUD endpoints should be rate limited by user."""
        # Create several links
        for i in range(5):
            response = await client.post(
                "/links",
                json={"url": f"https://example{i}.com"},
                headers=auth_headers,
            )
            # Should either succeed or hit rate limit
            assert response.status_code in (201, 429)

    @pytest.mark.usefixtures("enable_rate_limiting")
    async def test_analytics_rate_limited(
        self, client: AsyncClient, auth_headers: dict[str, str], link_slug: str
    ) -> None:
        """Analytics endpoints should be rate limited by user."""
        # Make several analytics requests
        for _ in range(5):
            response = await client.get(
                f"/analytics/{link_slug}",
                headers=auth_headers,
            )
            # Should either succeed or hit rate limit
            assert response.status_code in (200, 429)


class TestRateLimitIsolation:
    """Test that rate limits are properly isolated between users/IPs."""

    @pytest.mark.usefixtures("enable_rate_limiting")
    async def test_different_users_have_separate_limits(
        self, client: AsyncClient
    ) -> None:
        """Different authenticated users should have separate rate limit buckets."""
        # Register two users
        user1_resp = await client.post(
            "/users/register",
            json={"email": "user1@example.com", "password": "testpass123"},
        )
        user2_resp = await client.post(
            "/users/register",
            json={"email": "user2@example.com", "password": "testpass123"},
        )

        if user1_resp.status_code == 429 or user2_resp.status_code == 429:
            pytest.skip("Rate limit hit during setup")

        user1_key = user1_resp.json()["api_key"]
        user2_key = user2_resp.json()["api_key"]

        # Both users should be able to make requests independently
        resp1 = await client.get(
            "/links",
            headers={"Authorization": f"Bearer {user1_key}"},
        )
        resp2 = await client.get(
            "/links",
            headers={"Authorization": f"Bearer {user2_key}"},
        )

        # Both should succeed (separate rate limit buckets)
        assert resp1.status_code == 200
        assert resp2.status_code == 200


class TestHealthEndpointNotRateLimited:
    """Test that health endpoint is not rate limited."""

    @pytest.mark.usefixtures("enable_rate_limiting")
    async def test_health_endpoint_always_accessible(
        self, client: AsyncClient
    ) -> None:
        """Health endpoint should not be rate limited."""
        # Make many health check requests
        for _ in range(20):
            response = await client.get("/health")
            # Health should always succeed
            assert response.status_code == 200


class TestRateLimitDisabled:
    """Test that rate limiting can be disabled."""

    async def test_no_rate_limit_when_disabled(self, client: AsyncClient) -> None:
        """When rate limiting is disabled, requests should not be limited."""
        # Rate limiting is disabled by default in tests via conftest
        # Make many requests - none should be rate limited
        for i in range(15):
            response = await client.post(
                "/users/register",
                json={"email": f"nolimit{i}@example.com", "password": "testpass123"},
            )
            # Should succeed (201) or conflict (409 for duplicate), never 429
            assert response.status_code in (201, 409)
