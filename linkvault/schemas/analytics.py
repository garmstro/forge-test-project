from __future__ import annotations

from pydantic import BaseModel


class DayClicks(BaseModel):
    date: str  # ISO-8601 date string, e.g. "2025-01-15"
    clicks: int


class RefererClicks(BaseModel):
    referer: str
    clicks: int


class UserAgentClicks(BaseModel):
    user_agent: str
    clicks: int


class LinkAnalyticsResponse(BaseModel):
    slug: str
    total_clicks: int
    unique_ips: int
    clicks_by_day: list[DayClicks]
    top_referers: list[RefererClicks]
    top_user_agents: list[UserAgentClicks]


class TopLink(BaseModel):
    slug: str
    clicks: int


class SummaryResponse(BaseModel):
    total_links: int
    active_links: int
    total_clicks_all_time: int
    clicks_last_30_days: int
    top_link: TopLink | None

