from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.api.deps import get_current_user
from linkvault.database import get_db
from linkvault.models.user import User
from linkvault.schemas.link import PaginatedLinksResponse

router = APIRouter(prefix="/links", tags=["links"])


@router.get("", response_model=PaginatedLinksResponse)
async def list_links(
    page: int = 1,
    page_size: int = 20,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PaginatedLinksResponse:
    """Return a paginated list of the authenticated user's links (stub — Item 2)."""
    return PaginatedLinksResponse(total=0, page=page, page_size=page_size, items=[])

