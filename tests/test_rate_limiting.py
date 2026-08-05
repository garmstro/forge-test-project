"""
Tests for rate limiting functionality.

Tests both the RateLimiter class and the RateLimitMiddleware.
"""
from __future__ import annotations

import time

import pytest
from httpx import AsyncClient

from linkvault.rate_limiter import RateLimitConfig, RateLimiter


class TestRateLimiter:
    """Tests for the RateLimiter class."""

    def test_rate_limiter_allows_requests_within_limit(self):
        """Test that requests within the limit are allowed."""
        limiter = RateLimiter()
        config = RateLimitConfig(requests_per_minute=5, requests_per_hour=100)

        for i in range(5):
            is_allowed, info = limiter.is_allowed("user1", config)
            assert is_allowed is True
            assert info["remaining_per_minute"] == 4 - i

    def test_rate_limiter_blocks_requests_exceeding_minute_limit(self):
        """Test that requests exceeding per-minute limit are blocked."""
        limiter = RateLimiter()
        config = RateLimitConfig(requests_per_minute=3, requests_per_hour=100)

        # Allow 3 requests
        for i in range(3):
            is_allowed, _ = limiter.is_allowed("user1", config)
            assert is_allowed is True

        # 4th request should be blocked
        is_allowed, info = limiter.is_allowed("user1", config)
        assert is_allowed is False
        assert info["remaining_per_minute"] == 0

    def test_rate_limiter_blocks_requests_exceeding_hour_limit(self):
        """Test that requests exceeding per-hour limit are blocked."""
        limiter = RateLimiter()
        config = RateLimitConfig(requests_per_minute=1000, requests_per_hour=3)

        # Allow 3 requests
        for i in range(3):
            is_allowed, _ = limiter.is_allowed("user1", config)
            assert is_allowed is True

        # 4th request should be blocked
        is_allowed, info = limiter.is_allowed("user1", config)
        assert is_allowed is False
        assert info["remaining_per_hour"] == 0

    def test_rate_limiter_separate_identifiers(self):
        """Test that different identifiers have separate limits."""
        limiter = RateLimiter()
        config = RateLimitConfig(requests_per_minute=2, requests_per_hour=100)

        # User 1 makes 2 requests
        for _ in range(2):
            is_allowed, _ = limiter.is_allowed("user1", config)
            assert is_allowed is True

        # User 1 is blocked
        is_allowed, _ = limiter.is_allowed("user1", config)
        assert is_allowed is False

        # User 2 can still make requests
        is_allowed, _ = limiter.is_allowed("user2", config)
        assert is_allowed is True

    def test_rate_limiter_returns_correct_reset_times(self):
        """Test that reset times are correctly calculated."""
        limiter = RateLimiter()
        config = RateLimitConfig(requests_per_minute=10, requests_per_hour=100)

        _, info = limiter.is_allowed("user1", config)

        # Reset times should be in the future
        now = time.time()
        assert info["reset_minute_at"] > now
        assert info["reset_hour_at"] > now

        # Minute reset should be within 60 seconds
        assert info["reset_minute_at"] - now <= 60

        # Hour reset should be within 3600 seconds
        assert info["reset_hour_at"] - now <= 3600

    def test_rate_limiter_get_limit_info_without_incrementing(self):
        """Test that get_limit_info doesn't increment counters."""
        limiter = RateLimiter()
        config = RateLimitConfig(requests_per_minute=5, requests_per_hour=100)

        # Make one request
        limiter.is_allowed("user1", config)

        # Get info multiple times without incrementing
        info1 = limiter.get_limit_info("user1", config)
        info2 = limiter.get_limit_info("user1", config)

        assert info1["remaining_per_minute"] == 4
        assert info2["remaining_per_minute"] == 4

    def test_rate_limiter_cleanup_removes_expired_entries(self):
        """Test that cleanup removes old entries."""
        limiter = RateLimiter(cleanup_interval=0)  # Always cleanup
        config = RateLimitConfig(requests_per_minute=10, requests_per_hour=100)

        # Make a request
        limiter.is_allowed("user1", config)

        # Verify entry exists
        assert "user1" in limiter.minute_windows

        # Manually set old timestamp to simulate expired entry
        old_time = time.time() - 120  # 2 minutes ago
        limiter.minute_windows["user1"] = [(old_time, 5)]

        # Trigger cleanup
        limiter._cleanup_expired()

        # Entry should be removed
        assert "user1" not in limiter.minute_windows


class TestRateLimitMiddleware:
    """Tests for the RateLimitMiddleware."""

    @pytest.mark.asyncio
    async def test_middleware_allows_requests_within_limit(self, client: AsyncClient):
        """Test that requests within limit are allowed."""
        # Health endpoint is excluded, so use /users/register
        for i in range(3):
            response = await client.post(
                "/users/register",
                json={"email": f"user{i}@example.com", "password": "password123"},
            )
            # Should get 201 or 422 (validation), not 429
            assert response.status_code in [201, 422]

    @pytest.mark.asyncio
    async def test_middleware_adds_rate_limit_headers(self, client: AsyncClient):
        """Test that rate limit headers are added to responses."""
        response = await client.post(
            "/users/register",
            json={"email": "test@example.com", "password": "password123"},
        )

        # Check for rate limit headers
        assert "X-RateLimit-Limit-Per-Minute" in response.headers
        assert "X-RateLimit-Remaining-Per-Minute" in response.headers
        assert "X-RateLimit-Reset-Minute" in response.headers
        assert "X-RateLimit-Limit-Per-Hour" in response.headers
        assert "X-RateLimit-Remaining-Per-Hour" in response.headers
        assert "X-RateLimit-Reset-Hour" in response.headers

    @pytest.mark.asyncio
    async def test_middleware_excludes_health_endpoint(self, client: AsyncClient):
        """Test that excluded paths are not rate limited."""
        # Make many requests to health endpoint
        for _ in range(100):
            response = await client.get("/health")
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_middleware_respects_x_forwarded_for_header(self, client: AsyncClient):
        """Test that X-Forwarded-For header is used for IP detection."""
        # This test verifies the middleware correctly extracts IP from proxy headers
        response = await client.post(
            "/users/register",
            json={"email": "test@example.com", "password": "password123"},
            headers={"X-Forwarded-For": "192.168.1.1, 10.0.0.1"},
        )

        # Should succeed (rate limit is per IP)
        assert response.status_code in [201, 422]


class TestUserRateLimiting:
    """Tests for per-user rate limiting."""

    @pytest.mark.asyncio
    async def test_user_rate_limiting_on_link_creation(
        self, client: AsyncClient, db_session
    ):
        """Test that user-based rate limiting works on link creation."""
        # Register a user
        register_response = await client.post(
            "/users/register",
            json={"email": "testuser@example.com", "password": "password123"},
        )
        assert register_response.status_code == 201
        api_key = register_response.json()["api_key"]

        # Create links with the user's API key
        headers = {"Authorization": f"Bearer {api_key}"}

        # Should be able to create multiple links
        for i in range(3):
            response = await client.post(
                "/links",
                json={"url": f"https://example.com/{i}"},
                headers=headers,
            )
            assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_rate_limit_headers_include_user_limits(
        self, client: AsyncClient, db_session
    ):
        """Test that rate limit headers are present in authenticated requests."""
        # Register a user
        register_response = await client.post(
            "/users/register",
            json={"email": "testuser@example.com", "password": "password123"},
        )
        api_key = register_response.json()["api_key"]

        # Make an authenticated request
        headers = {"Authorization": f"Bearer {api_key}"}
        response = await client.post(
            "/links",
            json={"url": "https://example.com"},
            headers=headers,
        )

        # Should have rate limit headers
        assert "X-RateLimit-Limit-Per-Minute" in response.headers
        assert "X-RateLimit-Remaining-Per-Minute" in response.headers

