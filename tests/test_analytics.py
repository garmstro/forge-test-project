"""
Tests for the Analytics API (Step 4).

Covers:
- clicks_by_day groups correctly by UTC day
- unique_ips counts deduplicated
- top_referers sorted descending by clicks
- Summary counts only the authenticated user's links
- GET /analytics/{slug} requires ownership
- GET /analytics/summary aggregate totals
- days and tz query parameters
- 404 on missing slug
- 403 on non-owner access
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.models.click import Click
from linkvault.models.link import Link

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REGISTER_URL = "/users/register"
LINKS_URL = "/links"
ANALYTICS_URL = "/analytics"


async def register_and_get_key(
    client: AsyncClient, email: str, password: str = "password123"
) -> str:
    resp = await client.post(REGISTER_URL, json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()["api_key"]


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


async def create_link(
    client: AsyncClient,
    api_key: str,
    url: str = "https://example.com/",
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


async def _insert_click(
    db_session: AsyncSession,
    link_id: str,
    clicked_at: datetime,
    ip_address: str | None = "1.2.3.0",
    user_agent: str | None = "TestAgent/1.0",
    referer: str | None = None,
) -> None:
    """Directly insert a Click row for test setup."""
    click = Click(
        link_id=link_id,
        clicked_at=clicked_at,
        ip_address=ip_address,
        user_agent=user_agent,
        referer=referer,
    )
    db_session.add(click)
    await db_session.commit()


async def _get_link_id(db_session: AsyncSession, slug: str) -> str:
    from sqlalchemy import select

    result = await db_session.execute(
        select(Link.id).where(Link.slug == slug)
    )
    return result.scalar_one()


# ---------------------------------------------------------------------------
# GET /analytics/{slug} — basic functionality
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analytics_slug_returns_200(client: AsyncClient) -> None:
    key = await register_and_get_key(client, "analy1@example.com")
    await create_link(client, key, slug="analy1")

    resp = await client.get(f"{ANALYTICS_URL}/analy1", headers=auth(key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == "analy1"
    assert "total_clicks" in body
    assert "unique_ips" in body
    assert "clicks_by_day" in body
    assert "top_referers" in body
    assert "top_user_agents" in body


@pytest.mark.asyncio
async def test_analytics_slug_zero_clicks(client: AsyncClient) -> None:
    """A brand-new link with no clicks should return zeros."""
    key = await register_and_get_key(client, "analy_zero@example.com")
    await create_link(client, key, slug="zero-clicks")

    resp = await client.get(f"{ANALYTICS_URL}/zero-clicks", headers=auth(key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_clicks"] == 0
    assert body["unique_ips"] == 0
    assert body["top_referers"] == []
    assert body["top_user_agents"] == []


@pytest.mark.asyncio
async def test_analytics_slug_not_found(client: AsyncClient) -> None:
    key = await register_and_get_key(client, "analy_404@example.com")

    resp = await client.get(f"{ANALYTICS_URL}/no-such-slug", headers=auth(key))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_analytics_slug_forbidden_for_non_owner(client: AsyncClient) -> None:
    key_a = await register_and_get_key(client, "analy_owner@example.com")
    key_b = await register_and_get_key(client, "analy_intruder@example.com")
    await create_link(client, key_a, slug="owner-link")

    resp = await client.get(f"{ANALYTICS_URL}/owner-link", headers=auth(key_b))
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# clicks_by_day — grouping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clicks_by_day_groups_correctly(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """clicks_by_day must group clicks by UTC date correctly."""
    key = await register_and_get_key(client, "byday@example.com")
    await create_link(client, key, slug="byday")
    link_id = await _get_link_id(db_session, "byday")

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    day0 = now_utc.replace(hour=12, minute=0, second=0, microsecond=0)
    day1 = day0 - timedelta(days=1)
    day2 = day0 - timedelta(days=2)

    # Insert: 3 clicks today, 2 yesterday, 1 two days ago
    for _ in range(3):
        await _insert_click(db_session, link_id, day0)
    for _ in range(2):
        await _insert_click(db_session, link_id, day1)
    await _insert_click(db_session, link_id, day2)

    resp = await client.get(f"{ANALYTICS_URL}/byday?days=7", headers=auth(key))
    assert resp.status_code == 200
    body = resp.json()

    # Build a lookup of date -> clicks from the response
    day_map = {entry["date"]: entry["clicks"] for entry in body["clicks_by_day"]}

    today_key = day0.date().isoformat()
    yesterday_key = day1.date().isoformat()
    two_days_key = day2.date().isoformat()

    assert day_map.get(today_key, 0) == 3
    assert day_map.get(yesterday_key, 0) == 2
    assert day_map.get(two_days_key, 0) == 1
    assert body["total_clicks"] == 6


@pytest.mark.asyncio
async def test_clicks_by_day_length_matches_days_param(
    client: AsyncClient,
) -> None:
    """clicks_by_day must contain exactly *days* entries."""
    key = await register_and_get_key(client, "daylen@example.com")
    await create_link(client, key, slug="daylen")

    for days in (7, 14, 30):
        resp = await client.get(
            f"{ANALYTICS_URL}/daylen?days={days}", headers=auth(key)
        )
        assert resp.status_code == 200
        assert len(resp.json()["clicks_by_day"]) == days


@pytest.mark.asyncio
async def test_clicks_outside_window_excluded(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Clicks older than the lookback window must not appear in total_clicks."""
    key = await register_and_get_key(client, "window@example.com")
    await create_link(client, key, slug="window")
    link_id = await _get_link_id(db_session, "window")

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    # One click inside the 7-day window, one outside
    inside = now_utc - timedelta(days=3)
    outside = now_utc - timedelta(days=10)

    await _insert_click(db_session, link_id, inside)
    await _insert_click(db_session, link_id, outside)

    resp = await client.get(f"{ANALYTICS_URL}/window?days=7", headers=auth(key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_clicks"] == 1


# ---------------------------------------------------------------------------
# unique_ips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unique_ips_deduplicates(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """unique_ips must count distinct IP addresses, not total clicks."""
    key = await register_and_get_key(client, "uniqip@example.com")
    await create_link(client, key, slug="uniqip")
    link_id = await _get_link_id(db_session, "uniqip")

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    # 3 clicks from IP A, 2 from IP B, 1 from IP C → 3 unique IPs
    for _ in range(3):
        await _insert_click(db_session, link_id, now_utc, ip_address="10.0.0.0")
    for _ in range(2):
        await _insert_click(db_session, link_id, now_utc, ip_address="10.0.1.0")
    await _insert_click(db_session, link_id, now_utc, ip_address="10.0.2.0")

    resp = await client.get(f"{ANALYTICS_URL}/uniqip", headers=auth(key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_clicks"] == 6
    assert body["unique_ips"] == 3


# ---------------------------------------------------------------------------
# top_referers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_referers_sorted_descending(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """top_referers must be sorted by click count descending."""
    key = await register_and_get_key(client, "referer@example.com")
    await create_link(client, key, slug="referer")
    link_id = await _get_link_id(db_session, "referer")

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    # twitter: 5 clicks, facebook: 3 clicks, reddit: 1 click
    for _ in range(5):
        await _insert_click(db_session, link_id, now_utc, referer="https://twitter.com")
    for _ in range(3):
        await _insert_click(db_session, link_id, now_utc, referer="https://facebook.com")
    await _insert_click(db_session, link_id, now_utc, referer="https://reddit.com")

    resp = await client.get(f"{ANALYTICS_URL}/referer", headers=auth(key))
    assert resp.status_code == 200
    top = resp.json()["top_referers"]

    assert len(top) == 3
    assert top[0]["referer"] == "https://twitter.com"
    assert top[0]["clicks"] == 5
    assert top[1]["referer"] == "https://facebook.com"
    assert top[1]["clicks"] == 3
    assert top[2]["referer"] == "https://reddit.com"
    assert top[2]["clicks"] == 1

    # Verify descending order generally
    for i in range(len(top) - 1):
        assert top[i]["clicks"] >= top[i + 1]["clicks"]


# ---------------------------------------------------------------------------
# top_user_agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_user_agents_sorted_descending(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """top_user_agents must be sorted by click count descending."""
    key = await register_and_get_key(client, "ua@example.com")
    await create_link(client, key, slug="ua-test")
    link_id = await _get_link_id(db_session, "ua-test")

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    for _ in range(4):
        await _insert_click(db_session, link_id, now_utc, user_agent="Chrome/120")
    for _ in range(2):
        await _insert_click(db_session, link_id, now_utc, user_agent="Firefox/115")

    resp = await client.get(f"{ANALYTICS_URL}/ua-test", headers=auth(key))
    assert resp.status_code == 200
    top = resp.json()["top_user_agents"]

    assert top[0]["user_agent"] == "Chrome/120"
    assert top[0]["clicks"] == 4
    assert top[1]["user_agent"] == "Firefox/115"
    assert top[1]["clicks"] == 2


# ---------------------------------------------------------------------------
# Timezone bucketing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clicks_by_day_respects_timezone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A click at 01:00 UTC falls on the previous calendar day in UTC-5.

    We use a fixed UTC timestamp that is 01:00 on a specific day, then verify:
      - In UTC it lands on that day.
      - In America/New_York (UTC-5 in winter) it lands on the day before.

    The timestamp is chosen to be within the last 365 days from the sandbox
    clock so it always falls inside the lookback window.
    """
    key = await register_and_get_key(client, "tztest@example.com")
    await create_link(client, key, slug="tztest")
    link_id = await _get_link_id(db_session, "tztest")

    # Pick a "recent" date: 30 days ago at 01:00 UTC.  This is always within
    # any reasonable lookback window.
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    base_day = (now_utc - timedelta(days=30)).replace(
        hour=1, minute=0, second=0, microsecond=0
    )
    # base_day is e.g. 2026-04-09 01:00 UTC
    # In UTC-5 that is 2026-04-08 20:00 — so the UTC date is base_day.date()
    # and the UTC-5 date is (base_day - 5h).date() == base_day.date() - 1 day.
    utc_date = base_day.date().isoformat()
    ny_date = (base_day - timedelta(hours=5)).date().isoformat()

    await _insert_click(db_session, link_id, base_day)

    # In UTC the click should land on base_day.date()
    resp_utc = await client.get(
        f"{ANALYTICS_URL}/tztest?days=60&tz=UTC", headers=auth(key)
    )
    assert resp_utc.status_code == 200
    day_map_utc = {
        e["date"]: e["clicks"] for e in resp_utc.json()["clicks_by_day"]
    }
    assert day_map_utc.get(utc_date, 0) == 1
    assert day_map_utc.get(ny_date, 0) == 0

    # In America/New_York (UTC-5 in winter) the same click lands on ny_date
    resp_ny = await client.get(
        f"{ANALYTICS_URL}/tztest?days=60&tz=America%2FNew_York", headers=auth(key)
    )
    assert resp_ny.status_code == 200
    day_map_ny = {
        e["date"]: e["clicks"] for e in resp_ny.json()["clicks_by_day"]
    }
    assert day_map_ny.get(ny_date, 0) == 1
    assert day_map_ny.get(utc_date, 0) == 0


@pytest.mark.asyncio
async def test_invalid_timezone_returns_422(client: AsyncClient) -> None:
    key = await register_and_get_key(client, "badtz@example.com")
    await create_link(client, key, slug="badtz")

    resp = await client.get(
        f"{ANALYTICS_URL}/badtz?tz=Not%2FA%2FTimezone", headers=auth(key)
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /analytics/summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_counts_only_own_links(client: AsyncClient) -> None:
    """Summary must only count links belonging to the authenticated user."""
    key_a = await register_and_get_key(client, "sum_a@example.com")
    key_b = await register_and_get_key(client, "sum_b@example.com")

    # User A creates 3 links
    for i in range(3):
        await create_link(client, key_a, slug=f"sum-a-{i}")

    # User B creates 1 link
    await create_link(client, key_b, slug="sum-b-0")

    resp_a = await client.get(f"{ANALYTICS_URL}/summary", headers=auth(key_a))
    assert resp_a.status_code == 200
    body_a = resp_a.json()
    assert body_a["total_links"] == 3

    resp_b = await client.get(f"{ANALYTICS_URL}/summary", headers=auth(key_b))
    assert resp_b.status_code == 200
    body_b = resp_b.json()
    assert body_b["total_links"] == 1


@pytest.mark.asyncio
async def test_summary_structure(client: AsyncClient) -> None:
    """Summary response must include all required fields."""
    key = await register_and_get_key(client, "sum_struct@example.com")

    resp = await client.get(f"{ANALYTICS_URL}/summary", headers=auth(key))
    assert resp.status_code == 200
    body = resp.json()
    assert "total_links" in body
    assert "active_links" in body
    assert "total_clicks_all_time" in body
    assert "clicks_last_30_days" in body
    assert "top_link" in body


@pytest.mark.asyncio
async def test_summary_no_links(client: AsyncClient) -> None:
    """A fresh user with no links should get all-zero summary."""
    key = await register_and_get_key(client, "sum_empty@example.com")

    resp = await client.get(f"{ANALYTICS_URL}/summary", headers=auth(key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_links"] == 0
    assert body["active_links"] == 0
    assert body["total_clicks_all_time"] == 0
    assert body["clicks_last_30_days"] == 0
    assert body["top_link"] is None


@pytest.mark.asyncio
async def test_summary_total_clicks_all_time(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """total_clicks_all_time must sum click_count across all user links."""
    key = await register_and_get_key(client, "sum_clicks@example.com")
    link1 = await create_link(client, key, slug="sum-c1")
    link2 = await create_link(client, key, slug="sum-c2")

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    # Insert 5 clicks on link1 and 3 on link2 via redirect (updates click_count atomically)
    for _ in range(5):
        await _insert_click(db_session, link1["id"], now_utc)
    for _ in range(3):
        await _insert_click(db_session, link2["id"], now_utc)

    # Also update click_count directly to mirror what the redirect engine does
    from sqlalchemy import update as sa_update
    await db_session.execute(
        sa_update(Link).where(Link.id == link1["id"]).values(click_count=5)
    )
    await db_session.execute(
        sa_update(Link).where(Link.id == link2["id"]).values(click_count=3)
    )
    await db_session.commit()

    resp = await client.get(f"{ANALYTICS_URL}/summary", headers=auth(key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_clicks_all_time"] == 8


@pytest.mark.asyncio
async def test_summary_clicks_last_30_days(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """clicks_last_30_days must only count clicks within the last 30 days."""
    key = await register_and_get_key(client, "sum_30d@example.com")
    link = await create_link(client, key, slug="sum-30d")
    link_id = link["id"]

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    recent = now_utc - timedelta(days=5)
    old = now_utc - timedelta(days=45)

    await _insert_click(db_session, link_id, recent)
    await _insert_click(db_session, link_id, recent)
    await _insert_click(db_session, link_id, old)  # outside window

    resp = await client.get(f"{ANALYTICS_URL}/summary", headers=auth(key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["clicks_last_30_days"] == 2


@pytest.mark.asyncio
async def test_summary_top_link(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """top_link must identify the link with the highest click_count."""
    key = await register_and_get_key(client, "sum_top@example.com")
    link_a = await create_link(client, key, slug="top-a")
    link_b = await create_link(client, key, slug="top-b")

    from sqlalchemy import update as sa_update

    await db_session.execute(
        sa_update(Link).where(Link.id == link_a["id"]).values(click_count=10)
    )
    await db_session.execute(
        sa_update(Link).where(Link.id == link_b["id"]).values(click_count=99)
    )
    await db_session.commit()

    resp = await client.get(f"{ANALYTICS_URL}/summary", headers=auth(key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["top_link"] is not None
    assert body["top_link"]["slug"] == "top-b"
    assert body["top_link"]["clicks"] == 99


@pytest.mark.asyncio
async def test_summary_active_vs_expired_links(
    client: AsyncClient,
) -> None:
    """active_links must exclude expired or capped links."""
    key = await register_and_get_key(client, "sum_active@example.com")

    # 1 active link (no expiry, no cap)
    await create_link(client, key, slug="active-link")

    # 1 expired link
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    await create_link(client, key, slug="expired-link", expires_at=past)

    resp = await client.get(f"{ANALYTICS_URL}/summary", headers=auth(key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_links"] == 2
    assert body["active_links"] == 1


@pytest.mark.asyncio
async def test_summary_requires_auth(client: AsyncClient) -> None:
    resp = await client.get(f"{ANALYTICS_URL}/summary")
    assert resp.status_code in (401, 403)

