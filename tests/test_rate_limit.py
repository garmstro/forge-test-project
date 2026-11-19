"""Tests for application-level rate limiting."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from linkvault.config import settings
from linkvault.services import rate_limiter
from linkvault.services.rate_limiter import SlidingWindowLimiter

TOKEN_URL = "/users/token"
BAD_LOGIN = {"email": "nobody@example.com", "password": "password123"}


async def _register(client: AsyncClient, email: str = "rl@example.com") -> str:
    resp = await client.post(
        "/users/register", json={"email": email, "password": "password123"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["api_key"]


@pytest.mark.asyncio
async def test_token_endpoint_returns_429_with_retry_after(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_AUTH_PER_MINUTE", 3)

    for _ in range(3):
        resp = await client.post(TOKEN_URL, json=BAD_LOGIN)
        assert resp.status_code == 401

    resp = await client.post(TOKEN_URL, json=BAD_LOGIN)
    assert resp.status_code == 429
    assert resp.json() == {"detail": "Rate limit exceeded. Try again later."}
    retry_after = int(resp.headers["Retry-After"])
    assert 1 <= retry_after <= 60


@pytest.mark.asyncio
async def test_links_limited_independently_of_auth(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = await _register(client)
    headers = {"Authorization": f"Bearer {key}"}

    monkeypatch.setattr(settings, "RATE_LIMIT_AUTH_PER_MINUTE", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_API_PER_MINUTE", 2)
    rate_limiter.limiter.reset()

    # Exhaust the auth scope.
    assert (await client.post(TOKEN_URL, json=BAD_LOGIN)).status_code == 401
    assert (await client.post(TOKEN_URL, json=BAD_LOGIN)).status_code == 429

    # The api scope is unaffected, until its own limit is reached.
    assert (await client.get("/links", headers=headers)).status_code == 200
    assert (await client.get("/links", headers=headers)).status_code == 200
    resp = await client.get("/links", headers=headers)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


@pytest.mark.asyncio
async def test_redirects_are_limited(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = await _register(client)
    resp = await client.post(
        "/links",
        json={"url": "https://example.com/dest", "slug": "rl-slug"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 201, resp.text

    monkeypatch.setattr(settings, "RATE_LIMIT_REDIRECT_PER_MINUTE", 2)
    rate_limiter.limiter.reset()

    for _ in range(2):
        assert (await client.get("/rl-slug", follow_redirects=False)).status_code == 301

    resp = await client.get("/rl-slug", follow_redirects=False)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


@pytest.mark.asyncio
async def test_disabled_flag_disables_limiting(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_AUTH_PER_MINUTE", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)

    for _ in range(5):
        assert (await client.post(TOKEN_URL, json=BAD_LOGIN)).status_code == 401


@pytest.mark.asyncio
async def test_window_expires(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(
        rate_limiter, "time", SimpleNamespace(monotonic=lambda: now[0])
    )
    monkeypatch.setattr(settings, "RATE_LIMIT_AUTH_PER_MINUTE", 2)

    assert (await client.post(TOKEN_URL, json=BAD_LOGIN)).status_code == 401
    now[0] += 10
    assert (await client.post(TOKEN_URL, json=BAD_LOGIN)).status_code == 401
    resp = await client.post(TOKEN_URL, json=BAD_LOGIN)
    assert resp.status_code == 429
    # Oldest hit is 10s old, so about 50s remain.
    assert resp.headers["Retry-After"] == "50"

    now[0] += 51  # first hit has left the window
    assert (await client.post(TOKEN_URL, json=BAD_LOGIN)).status_code == 401
    assert (await client.post(TOKEN_URL, json=BAD_LOGIN)).status_code == 429

    now[0] += 61  # everything expired
    assert (await client.post(TOKEN_URL, json=BAD_LOGIN)).status_code == 401


def test_limiter_prunes_stale_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(
        rate_limiter, "time", SimpleNamespace(monotonic=lambda: now[0])
    )
    lim = SlidingWindowLimiter()
    for i in range(10):
        assert lim.check(f"scope:{i}", 5) == (True, 0)
    assert len(lim._hits) == 10

    now[0] += 7200
    for _ in range(rate_limiter._PRUNE_EVERY):
        lim.check("scope:live", 10**9)
    assert list(lim._hits) == ["scope:live"]
