"""
Tests for the Redirect Engine (Step 3).

Covers:
- Valid slug redirects to destination URL
- Unknown slug returns 404
- Expired link (past expires_at) returns 410
- Link at max_clicks returns 410
- Click event is recorded on valid redirect
- Redirect response time is under 50 ms (integration test)
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

REGISTER_URL = "/users/register"
LINKS_URL = "/links"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def register_and_get_key(client: AsyncClient, email: str, password: str = "password123") -> str:
    resp = await client.post(REGISTER_URL, json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()["api_key"]


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


async def create_link(
    client: AsyncClient,
    api_key: str,
    url: str = "https://example.com/destination",
    slug: str | None = None,
    expires_at: str | None = None,
    max_clicks: int | None = None,
) -> dict:
    payload: dict = {"url": url}
    if slug is not None:
        payload["slug"] = slug
    if expires_at is not None:
        payload["expires_at"] = expires_at
    if max_clicks is not None:
        payload["max_clicks"] = max_clicks
    resp = await client.post(LINKS_URL, json=payload, headers=auth(api_key))
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Basic redirect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redirect_valid_slug(client: AsyncClient) -> None:
    """A valid slug should redirect to the destination URL."""
    key = await register_and_get_key(client, "redir1@example.com")
    link = await create_link(client, key, url="https://example.com/dest", slug="redir1")

    resp = await client.get("/redir1", follow_redirects=False)
    assert resp.status_code in (301, 302)
    assert resp.headers["location"] == "https://example.com/dest"


@pytest.mark.asyncio
async def test_redirect_permanent_for_plain_link(client: AsyncClient) -> None:
    """A link with no expiry and no click cap should return 301."""
    key = await register_and_get_key(client, "perm1@example.com")
    await create_link(client, key, url="https://permanent.example.com/", slug="perm1")

    resp = await client.get("/perm1", follow_redirects=False)
    assert resp.status_code == 301


@pytest.mark.asyncio
async def test_redirect_temporary_for_link_with_expiry(client: AsyncClient) -> None:
    """A link with expires_at set should return 302."""
    key = await register_and_get_key(client, "temp1@example.com")
    future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await create_link(client, key, url="https://temp.example.com/", slug="temp1", expires_at=future)

    resp = await client.get("/temp1", follow_redirects=False)
    assert resp.status_code == 302


@pytest.mark.asyncio
async def test_redirect_temporary_for_link_with_max_clicks(client: AsyncClient) -> None:
    """A link with max_clicks set should return 302 (while under the cap)."""
    key = await register_and_get_key(client, "temp2@example.com")
    await create_link(client, key, url="https://temp2.example.com/", slug="temp2", max_clicks=100)

    resp = await client.get("/temp2", follow_redirects=False)
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# 404 — unknown slug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redirect_unknown_slug_returns_404(client: AsyncClient) -> None:
    resp = await client.get("/no-such-slug-xyz", follow_redirects=False)
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "not_found"


@pytest.mark.asyncio
async def test_redirect_deleted_slug_returns_404(client: AsyncClient) -> None:
    """A soft-deleted link should not be redirectable."""
    key = await register_and_get_key(client, "del1@example.com")
    await create_link(client, key, url="https://example.com/", slug="del1")
    await client.delete(f"{LINKS_URL}/del1", headers=auth(key))

    resp = await client.get("/del1", follow_redirects=False)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 410 — expired link
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redirect_expired_link_returns_410(client: AsyncClient) -> None:
    """A link whose expires_at is in the past should return 410."""
    key = await register_and_get_key(client, "exp1@example.com")
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await create_link(client, key, url="https://expired.example.com/", slug="exp1", expires_at=past)

    resp = await client.get("/exp1", follow_redirects=False)
    assert resp.status_code == 410
    body = resp.json()
    assert body["error"] == "link_expired"


@pytest.mark.asyncio
async def test_redirect_max_clicks_reached_returns_410(client: AsyncClient) -> None:
    """A link that has hit its max_clicks cap should return 410."""
    key = await register_and_get_key(client, "maxc1@example.com")
    # Create with max_clicks=1, then hit it once to exhaust the cap.
    await create_link(client, key, url="https://maxclicks.example.com/", slug="maxc1", max_clicks=1)

    # First redirect — consumes the single allowed click.
    r1 = await client.get("/maxc1", follow_redirects=False)
    assert r1.status_code == 302

    # Second redirect — cap reached.
    r2 = await client.get("/maxc1", follow_redirects=False)
    assert r2.status_code == 410
    assert r2.json()["error"] == "link_expired"


# ---------------------------------------------------------------------------
# Click event recording
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_click_event_increments_click_count(client: AsyncClient) -> None:
    """Each successful redirect should increment click_count on the link."""
    key = await register_and_get_key(client, "click1@example.com")
    await create_link(client, key, url="https://example.com/", slug="click1")

    # Before any redirect
    before = await client.get(f"{LINKS_URL}/click1", headers=auth(key))
    assert before.json()["click_count"] == 0

    # Perform redirect
    await client.get("/click1", follow_redirects=False)

    # After redirect
    after = await client.get(f"{LINKS_URL}/click1", headers=auth(key))
    assert after.json()["click_count"] == 1


@pytest.mark.asyncio
async def test_click_count_increments_atomically(client: AsyncClient) -> None:
    """Multiple sequential redirects should each increment click_count by 1."""
    key = await register_and_get_key(client, "click2@example.com")
    await create_link(client, key, url="https://example.com/", slug="click2")

    for _ in range(5):
        await client.get("/click2", follow_redirects=False)

    resp = await client.get(f"{LINKS_URL}/click2", headers=auth(key))
    assert resp.json()["click_count"] == 5


@pytest.mark.asyncio
async def test_no_click_recorded_for_expired_link(client: AsyncClient) -> None:
    """An expired link should not have its click_count incremented."""
    key = await register_and_get_key(client, "noclick1@example.com")
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await create_link(client, key, url="https://example.com/", slug="noclick1", expires_at=past)

    await client.get("/noclick1", follow_redirects=False)

    resp = await client.get(f"{LINKS_URL}/noclick1", headers=auth(key))
    assert resp.json()["click_count"] == 0


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redirect_response_time_under_50ms(client: AsyncClient) -> None:
    """End-to-end redirect (including DB query) must complete in < 50 ms."""
    key = await register_and_get_key(client, "perf1@example.com")
    await create_link(client, key, url="https://perf.example.com/", slug="perf1")

    t0 = time.monotonic()
    resp = await client.get("/perf1", follow_redirects=False)
    elapsed_ms = (time.monotonic() - t0) * 1_000

    assert resp.status_code in (301, 302)
    assert elapsed_ms < 50, f"Redirect took {elapsed_ms:.1f} ms — exceeds 50 ms threshold"

