"""
Tests for application-level rate limiting (DECISIONS.md §5 reversal).

Strategy
--------
The in-memory rate-limit counters are shared across the entire test process
because `limiter` and `redirect_limiter` are module-level singletons.  To
keep tests isolated we:

1. Use very low limits (e.g. "2/minute") injected via monkeypatching the
   settings values and re-creating the limiter storage between tests.
2. Alternatively, drive the default limits to a very low number by patching
   `settings.RATE_LIMIT_API` / `settings.RATE_LIMIT_REDIRECT` before the
   decorated functions are called.

Because slowapi evaluates the limit string at call time (not at decoration
time), we can patch `settings.RATE_LIMIT_API` in a fixture and the decorator
`@limiter.limit(settings.RATE_LIMIT_API)` will pick up the new value.

Each test uses a unique IP / Bearer token so counters don't bleed between
tests even when the storage is shared.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

REGISTER_URL = "/users/register"
TOKEN_URL = "/users/token"
LINKS_URL = "/links"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def register_and_get_key(
    client: AsyncClient,
    email: str,
    password: str = "password123",
) -> str:
    resp = await client.post(REGISTER_URL, json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()["api_key"]


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# Fixtures — patch limits to tiny values so tests run fast
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def patch_rate_limits(monkeypatch):
    """
    Set very tight limits for the duration of each test so we can trigger
    429s with just a handful of requests.

    "3/minute" means: allow 3 requests per minute per key.
    """
    import linkvault.config as cfg_module
    import linkvault.rate_limit as rl_module

    monkeypatch.setattr(cfg_module.settings, "RATE_LIMIT_API", "3/minute")
    monkeypatch.setattr(cfg_module.settings, "RATE_LIMIT_REDIRECT", "3/minute")

    # Reset the in-memory storage so counters from previous tests don't bleed.
    rl_module.limiter._storage.reset()  # type: ignore[attr-defined]
    rl_module.redirect_limiter._storage.reset()  # type: ignore[attr-defined]

    yield

    # Clean up after the test as well.
    rl_module.limiter._storage.reset()  # type: ignore[attr-defined]
    rl_module.redirect_limiter._storage.reset()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# API rate limiting — authenticated endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_rate_limit_triggers_429_on_links(client: AsyncClient) -> None:
    """Exceeding the API rate limit on GET /links returns 429."""
    key = await register_and_get_key(client, "rl_links@example.com")

    # The first 3 requests should succeed (limit is 3/minute).
    # Registration already consumed 1 request from the IP bucket, but the
    # subsequent authenticated requests use the user-keyed bucket, so we
    # get 3 fresh slots.
    for i in range(3):
        resp = await client.get(LINKS_URL, headers=auth(key))
        assert resp.status_code == 200, f"Request {i+1} unexpectedly failed: {resp.text}"

    # The 4th request should be rate-limited.
    resp = await client.get(LINKS_URL, headers=auth(key))
    assert resp.status_code == 429
    body = resp.json()
    assert body["error"] == "rate_limit_exceeded"
    assert "detail" in body


@pytest.mark.asyncio
async def test_api_rate_limit_response_has_retry_after_header(client: AsyncClient) -> None:
    """A 429 response must include a Retry-After header."""
    key = await register_and_get_key(client, "rl_header@example.com")

    for _ in range(3):
        await client.get(LINKS_URL, headers=auth(key))

    resp = await client.get(LINKS_URL, headers=auth(key))
    assert resp.status_code == 429
    assert "retry-after" in resp.headers


@pytest.mark.asyncio
async def test_api_rate_limit_different_users_have_independent_buckets(
    client: AsyncClient,
) -> None:
    """Two different authenticated users must not share a rate-limit bucket."""
    key_a = await register_and_get_key(client, "rl_usera@example.com")
    key_b = await register_and_get_key(client, "rl_userb@example.com")

    # Exhaust user A's bucket.
    for _ in range(3):
        await client.get(LINKS_URL, headers=auth(key_a))

    # User A is now rate-limited.
    resp_a = await client.get(LINKS_URL, headers=auth(key_a))
    assert resp_a.status_code == 429

    # User B should still have a full bucket.
    resp_b = await client.get(LINKS_URL, headers=auth(key_b))
    assert resp_b.status_code == 200


@pytest.mark.asyncio
async def test_api_rate_limit_unauthenticated_falls_back_to_ip(
    client: AsyncClient,
) -> None:
    """Unauthenticated requests (e.g. /users/register) are bucketed by IP."""
    # We can only send 3 register requests before hitting the limit.
    # Use unique emails to avoid 409 conflicts.
    for i in range(3):
        resp = await client.post(
            REGISTER_URL,
            json={"email": f"rl_ip_{i}@example.com", "password": "password123"},
        )
        # 201 or 409 (duplicate) are both fine — we just care about 429 later.
        assert resp.status_code in (201, 409), resp.text

    # 4th request from the same IP should be rate-limited.
    resp = await client.post(
        REGISTER_URL,
        json={"email": "rl_ip_overflow@example.com", "password": "password123"},
    )
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_api_rate_limit_on_create_link(client: AsyncClient) -> None:
    """POST /links is also rate-limited."""
    key = await register_and_get_key(client, "rl_create@example.com")

    for i in range(3):
        resp = await client.post(
            LINKS_URL,
            json={"url": f"https://example.com/{i}"},
            headers=auth(key),
        )
        assert resp.status_code == 201, f"Request {i+1} failed: {resp.text}"

    resp = await client.post(
        LINKS_URL,
        json={"url": "https://example.com/overflow"},
        headers=auth(key),
    )
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Redirect rate limiting — public endpoint, IP-keyed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redirect_rate_limit_triggers_429(client: AsyncClient) -> None:
    """Exceeding the redirect rate limit returns 429."""
    # Create a link to redirect to.
    key = await register_and_get_key(client, "rl_redir@example.com")
    resp = await client.post(
        LINKS_URL,
        json={"url": "https://example.com/dest", "slug": "rl-redir"},
        headers=auth(key),
    )
    assert resp.status_code == 201

    # Reset storage so the registration requests don't count against the
    # redirect bucket (they use the API limiter, not the redirect limiter,
    # but the redirect_limiter storage was already reset by the fixture).
    import linkvault.rate_limit as rl_module
    rl_module.redirect_limiter._storage.reset()  # type: ignore[attr-defined]

    # First 3 redirects should succeed.
    for i in range(3):
        resp = await client.get("/rl-redir", follow_redirects=False)
        assert resp.status_code in (301, 302), f"Redirect {i+1} failed: {resp.text}"

    # 4th redirect should be rate-limited.
    resp = await client.get("/rl-redir", follow_redirects=False)
    assert resp.status_code == 429
    body = resp.json()
    assert body["error"] == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_redirect_rate_limit_response_has_retry_after_header(
    client: AsyncClient,
) -> None:
    """A rate-limited redirect response must include Retry-After."""
    key = await register_and_get_key(client, "rl_redir_hdr@example.com")
    await client.post(
        LINKS_URL,
        json={"url": "https://example.com/dest", "slug": "rl-redir-hdr"},
        headers=auth(key),
    )

    import linkvault.rate_limit as rl_module
    rl_module.redirect_limiter._storage.reset()  # type: ignore[attr-defined]

    for _ in range(3):
        await client.get("/rl-redir-hdr", follow_redirects=False)

    resp = await client.get("/rl-redir-hdr", follow_redirects=False)
    assert resp.status_code == 429
    assert "retry-after" in resp.headers


# ---------------------------------------------------------------------------
# 429 response shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_error_body_shape(client: AsyncClient) -> None:
    """The 429 body must have 'error' and 'detail' keys."""
    key = await register_and_get_key(client, "rl_shape@example.com")

    for _ in range(3):
        await client.get(LINKS_URL, headers=auth(key))

    resp = await client.get(LINKS_URL, headers=auth(key))
    assert resp.status_code == 429
    body = resp.json()
    assert set(body.keys()) >= {"error", "detail"}
    assert body["error"] == "rate_limit_exceeded"
    assert isinstance(body["detail"], str)
    assert len(body["detail"]) > 0
