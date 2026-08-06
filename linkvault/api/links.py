from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.api.deps import check_rate_limit_by_user, get_current_user
from linkvault.config import settings
from linkvault.database import get_db
from linkvault.models.link import Link
from linkvault.models.user import User
from linkvault.schemas.link import (
    LinkCreate,
    LinkResponse,
    LinkUpdate,
    PaginatedLinksResponse,
)
from linkvault.services.shortener import (
    MAX_RETRIES,
    generate_slug,
    is_reserved_slug,
    is_valid_custom_slug,
)

router = APIRouter(prefix="/links", tags=["links"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_active_link(slug: str, user: User, db: AsyncSession) -> Link:
    """Return the active (non-deleted) link owned by *user* with *slug*, or raise 404/403."""
    result = await db.execute(
        select(Link).where(Link.slug == slug, Link.deleted_at.is_(None))
    )
    link = result.scalar_one_or_none()
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Link not found.",
        )
    if link.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not own this link.",
        )
    return link


# ---------------------------------------------------------------------------
# POST /links — create a new short link
# ---------------------------------------------------------------------------


@router.post("", response_model=LinkResponse, status_code=status.HTTP_201_CREATED)
async def create_link(
    payload: LinkCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LinkResponse:
    """Create a new short link for the authenticated user."""
    # Rate limit: 100 requests per hour per user
    await check_rate_limit_by_user(
        current_user,
        max_requests=settings.RATE_LIMIT_CREATE_LINK_PER_HOUR,
        window_seconds=3600,
    )

    # ---- Determine slug -----------------------------------------------
    if payload.slug is not None:
        # Custom slug validation
        if not is_valid_custom_slug(payload.slug):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Slug must be 3–64 characters: alphanumeric and hyphens only.",
            )
        if is_reserved_slug(payload.slug):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"'{payload.slug}' is a reserved word and cannot be used as a slug.",
            )
        # Check uniqueness (case-insensitive via lower-case storage)
        slug = payload.slug.lower()
        existing = await db.execute(
            select(Link).where(Link.slug == slug, Link.deleted_at.is_(None))
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Slug '{slug}' is already in use.",
            )
    else:
        # Auto-generate slug with up to MAX_RETRIES collision retries
        slug: str | None = None  # type: ignore[no-redef]
        for _ in range(MAX_RETRIES):
            candidate = generate_slug()
            existing = await db.execute(
                select(Link).where(Link.slug == candidate, Link.deleted_at.is_(None))
            )
            if existing.scalar_one_or_none() is None:
                slug = candidate
                break
        if slug is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Could not generate a unique slug after several retries. Please try again.",
            )

    # ---- Persist ----------------------------------------------------------
    link = Link(
        user_id=current_user.id,
        slug=slug,
        destination_url=payload.url,
        expires_at=payload.expires_at,
        max_clicks=payload.max_clicks,
    )
    db.add(link)
    await db.flush()  # populate defaults (id, created_at) before reading

    return LinkResponse.model_validate(link)


# ---------------------------------------------------------------------------
# GET /links — paginated list of the authenticated user's links
# ---------------------------------------------------------------------------


@router.get("", response_model=PaginatedLinksResponse)
async def list_links(
    page: int = 1,
    page_size: int = 20,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PaginatedLinksResponse:
    """Return a paginated list of the authenticated user's active links."""
    if page < 1:
        page = 1
    page_size = min(max(page_size, 1), 100)

    base_query = select(Link).where(
        Link.user_id == current_user.id,
        Link.deleted_at.is_(None),
    )

    # Total count
    count_result = await db.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    total: int = count_result.scalar_one()

    # Paginated rows
    rows_result = await db.execute(
        base_query.order_by(Link.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    links = rows_result.scalars().all()

    return PaginatedLinksResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[LinkResponse.model_validate(lnk) for lnk in links],
    )


# ---------------------------------------------------------------------------
# GET /links/{slug} — full metadata for a single link
# ---------------------------------------------------------------------------


@router.get("/{slug}", response_model=LinkResponse)
async def get_link(
    slug: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LinkResponse:
    """Return full metadata for a single link owned by the authenticated user."""
    link = await _resolve_active_link(slug, current_user, db)
    return LinkResponse.model_validate(link)


# ---------------------------------------------------------------------------
# PATCH /links/{slug} — update mutable fields
# ---------------------------------------------------------------------------


@router.patch("/{slug}", response_model=LinkResponse)
async def update_link(
    slug: str,
    payload: LinkUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LinkResponse:
    """Update url, expires_at, or max_clicks on an existing link.

    Slug and owner are immutable.
    """
    link = await _resolve_active_link(slug, current_user, db)

    if payload.url is not None:
        link.destination_url = payload.url
    if payload.expires_at is not None:
        link.expires_at = payload.expires_at
    if payload.max_clicks is not None:
        link.max_clicks = payload.max_clicks

    db.add(link)
    await db.flush()

    return LinkResponse.model_validate(link)


# ---------------------------------------------------------------------------
# DELETE /links/{slug} — soft-delete
# ---------------------------------------------------------------------------


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_link(
    slug: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete a link (sets deleted_at). The slug becomes immediately reusable."""
    link = await _resolve_active_link(slug, current_user, db)
    link.deleted_at = datetime.utcnow()
    db.add(link)
    await db.flush()

