from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.api.deps import check_user_rate_limit, get_current_user
from linkvault.database import get_db
from linkvault.models.link import Link
from linkvault.models.user import User
from linkvault.schemas.analytics import LinkAnalyticsResponse, SummaryResponse
from linkvault.services.analytics import get_link_analytics, get_user_summary

router = APIRouter(prefix="/analytics", tags=["analytics"])


# ---------------------------------------------------------------------------
# GET /analytics/summary  — must be registered BEFORE /{slug} to avoid
# the path parameter swallowing the literal "summary" segment.
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=SummaryResponse)
async def analytics_summary(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(check_user_rate_limit),
) -> SummaryResponse:
    """Return aggregate analytics across all of the authenticated user's links.
    
    Rate limited per authenticated user.
    """
    data = await get_user_summary(current_user.id, db)
    return SummaryResponse(**data)


# ---------------------------------------------------------------------------
# GET /analytics/{slug}
# ---------------------------------------------------------------------------


@router.get("/{slug}", response_model=LinkAnalyticsResponse)
async def analytics_for_slug(
    slug: str,
    days: int = Query(default=30, ge=1, le=365),
    tz: str = Query(default="UTC"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(check_user_rate_limit),
) -> LinkAnalyticsResponse:
    """Return per-link analytics for *slug* over the past *days* days.

    Only the owner of the link may access its analytics.
    
    Rate limited per authenticated user.
    """
    # Resolve the active link
    result = await db.execute(
        select(Link).where(Link.slug == slug, Link.deleted_at.is_(None))
    )
    link = result.scalar_one_or_none()

    if link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Link not found.",
        )

    if link.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not own this link.",
        )

    # Validate timezone
    try:
        data = await get_link_analytics(link, db, days=days, tz=tz)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )

    return LinkAnalyticsResponse(**data)
