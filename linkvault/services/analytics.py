"""Click aggregation service for the Analytics API."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.models.click import Click
from linkvault.models.link import Link


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _lookback_start(days: int) -> datetime:
    """Return a naive UTC datetime *days* ago (start of that day)."""
    cutoff = _utc_now() - timedelta(days=days)
    return cutoff


def _to_tz_date(dt_utc: datetime, tz_name: str) -> date:
    """Convert a naive UTC datetime to a date in the given IANA timezone."""
    import zoneinfo

    tz = zoneinfo.ZoneInfo(tz_name)
    aware = dt_utc.replace(tzinfo=timezone.utc)
    return aware.astimezone(tz).date()


# ---------------------------------------------------------------------------
# Per-link analytics
# ---------------------------------------------------------------------------


async def get_link_analytics(
    link: Link,
    db: AsyncSession,
    days: int = 30,
    tz: str = "UTC",
) -> dict[str, Any]:
    """Return aggregated analytics for a single *link* over the past *days* days.

    The ``tz`` parameter is an IANA timezone name used for day bucketing.
    """
    import zoneinfo

    # Validate timezone early — raises ZoneInfoNotFoundError on bad input
    try:
        zoneinfo.ZoneInfo(tz)
    except (zoneinfo.ZoneInfoNotFoundError, KeyError):
        raise ValueError(f"Unknown timezone: {tz!r}")

    cutoff = _lookback_start(days)

    # Base filter: clicks for this link within the lookback window
    base_filter = [
        Click.link_id == link.id,
        Click.clicked_at >= cutoff,
    ]

    # ------------------------------------------------------------------
    # Total clicks in window
    # ------------------------------------------------------------------
    total_result = await db.execute(
        select(func.count(Click.id)).where(*base_filter)
    )
    total_clicks: int = total_result.scalar_one() or 0

    # ------------------------------------------------------------------
    # Unique IPs in window
    # ------------------------------------------------------------------
    unique_result = await db.execute(
        select(func.count(func.distinct(Click.ip_address))).where(*base_filter)
    )
    unique_ips: int = unique_result.scalar_one() or 0

    # ------------------------------------------------------------------
    # Clicks by day (bucketed in the requested timezone)
    # ------------------------------------------------------------------
    clicks_rows_result = await db.execute(
        select(Click.clicked_at).where(*base_filter).order_by(Click.clicked_at)
    )
    all_timestamps: list[datetime] = [row[0] for row in clicks_rows_result.fetchall()]

    # Build a dict: date_str -> count
    day_counts: dict[str, int] = {}
    for ts in all_timestamps:
        d = _to_tz_date(ts, tz)
        key = d.isoformat()
        day_counts[key] = day_counts.get(key, 0) + 1

    # Fill in zeros for every day in the window that had no clicks
    clicks_by_day: list[dict[str, Any]] = []
    for i in range(days):
        d = (_utc_now() - timedelta(days=days - 1 - i))
        day_key = _to_tz_date(d, tz).isoformat()
        clicks_by_day.append({"date": day_key, "clicks": day_counts.get(day_key, 0)})

    # ------------------------------------------------------------------
    # Top referers (top 10, descending)
    # ------------------------------------------------------------------
    referer_result = await db.execute(
        select(Click.referer, func.count(Click.id).label("clicks"))
        .where(*base_filter)
        .group_by(Click.referer)
        .order_by(text("clicks DESC"))
        .limit(10)
    )
    top_referers = [
        {"referer": row.referer or "", "clicks": row.clicks}
        for row in referer_result.fetchall()
    ]

    # ------------------------------------------------------------------
    # Top user-agents (top 10, descending)
    # ------------------------------------------------------------------
    ua_result = await db.execute(
        select(Click.user_agent, func.count(Click.id).label("clicks"))
        .where(*base_filter)
        .group_by(Click.user_agent)
        .order_by(text("clicks DESC"))
        .limit(10)
    )
    top_user_agents = [
        {"user_agent": row.user_agent or "", "clicks": row.clicks}
        for row in ua_result.fetchall()
    ]

    return {
        "slug": link.slug,
        "total_clicks": total_clicks,
        "unique_ips": unique_ips,
        "clicks_by_day": clicks_by_day,
        "top_referers": top_referers,
        "top_user_agents": top_user_agents,
    }


# ---------------------------------------------------------------------------
# User-level summary
# ---------------------------------------------------------------------------


async def get_user_summary(
    user_id: str,
    db: AsyncSession,
) -> dict[str, Any]:
    """Return aggregate analytics across all links owned by *user_id*."""
    cutoff_30 = _lookback_start(30)

    # ------------------------------------------------------------------
    # Total links (ever created, not soft-deleted)
    # ------------------------------------------------------------------
    total_links_result = await db.execute(
        select(func.count(Link.id)).where(
            Link.user_id == user_id,
            Link.deleted_at.is_(None),
        )
    )
    total_links: int = total_links_result.scalar_one() or 0

    # ------------------------------------------------------------------
    # Active links: not deleted, not expired, not over click cap
    # ------------------------------------------------------------------
    now = _utc_now()
    # Fetch all non-deleted links and filter in Python (SQLite doesn't
    # support complex conditional expressions as cleanly across async)
    all_links_result = await db.execute(
        select(Link).where(
            Link.user_id == user_id,
            Link.deleted_at.is_(None),
        )
    )
    all_links = all_links_result.scalars().all()

    active_count = 0
    for lnk in all_links:
        expired = lnk.expires_at is not None and lnk.expires_at < now
        capped = lnk.max_clicks is not None and lnk.click_count >= lnk.max_clicks
        if not expired and not capped:
            active_count += 1

    # ------------------------------------------------------------------
    # Total clicks (all time) — sum of click_count across all user links
    # ------------------------------------------------------------------
    total_clicks_result = await db.execute(
        select(func.sum(Link.click_count)).where(
            Link.user_id == user_id,
            Link.deleted_at.is_(None),
        )
    )
    total_clicks_all_time: int = int(total_clicks_result.scalar_one() or 0)

    # ------------------------------------------------------------------
    # Clicks in last 30 days — query the clicks table
    # ------------------------------------------------------------------
    user_link_ids_result = await db.execute(
        select(Link.id).where(
            Link.user_id == user_id,
            Link.deleted_at.is_(None),
        )
    )
    user_link_ids = [row[0] for row in user_link_ids_result.fetchall()]

    clicks_30d: int = 0
    if user_link_ids:
        clicks_30d_result = await db.execute(
            select(func.count(Click.id)).where(
                Click.link_id.in_(user_link_ids),
                Click.clicked_at >= cutoff_30,
            )
        )
        clicks_30d = int(clicks_30d_result.scalar_one() or 0)

    # ------------------------------------------------------------------
    # Top link by click_count
    # ------------------------------------------------------------------
    top_link: dict[str, Any] | None = None
    if all_links:
        best = max(all_links, key=lambda lnk: lnk.click_count)
        if best.click_count > 0:
            top_link = {"slug": best.slug, "clicks": best.click_count}

    return {
        "total_links": total_links,
        "active_links": active_count,
        "total_clicks_all_time": total_clicks_all_time,
        "clicks_last_30_days": clicks_30d,
        "top_link": top_link,
    }

