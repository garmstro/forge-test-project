from __future__ import annotations

import time
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.database import get_db
from linkvault.models.click import Click
from linkvault.models.link import Link
from linkvault.ratelimit import RATE_LIMIT_REDIRECTS, ip_limiter

router = APIRouter(tags=["redirects"])


def _anonymize_ip(ip: str | None) -> str | None:
    """Zero the last octet of an IPv4 address for privacy.

    IPv6 addresses are returned unchanged (full anonymisation is out of scope
    for this phase — see DECISIONS.md §5).
    """
    if ip is None:
        return None
    parts = ip.split(".")
    if len(parts) == 4:
        parts[-1] = "0"
        return ".".join(parts)
    return ip


@router.get("/{slug}", response_model=None)
@ip_limiter.limit(RATE_LIMIT_REDIRECTS)
async def redirect(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse | JSONResponse:
    """Resolve *slug* → destination URL and record a click event.

    Status codes:
    - 301  Permanent redirect (no expiry, no click cap).
    - 302  Temporary redirect (link has an expiry or click cap).
    - 404  Slug not found.
    - 410  Link expired or click cap reached.
    """
    # ------------------------------------------------------------------
    # Single indexed lookup — must complete in < 10 ms at the DB level.
    # ------------------------------------------------------------------
    t0 = time.monotonic()

    result = await db.execute(
        select(Link).where(Link.slug == slug, Link.deleted_at.is_(None))
    )
    link: Link | None = result.scalar_one_or_none()

    elapsed_ms = (time.monotonic() - t0) * 1_000
    if elapsed_ms > 10:
        import logging
        logging.getLogger(__name__).warning(
            "Redirect DB lookup took %.2f ms (> 10 ms threshold) for slug=%s",
            elapsed_ms,
            slug,
        )

    if link is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found"},
        )

    # ------------------------------------------------------------------
    # Expiry / click-cap check
    # ------------------------------------------------------------------
    now = datetime.utcnow()

    if link.expires_at is not None and link.expires_at < now:
        return JSONResponse(
            status_code=410,
            content={"error": "link_expired"},
        )

    if link.max_clicks is not None and link.click_count >= link.max_clicks:
        return JSONResponse(
            status_code=410,
            content={"error": "link_expired"},
        )

    # ------------------------------------------------------------------
    # Atomic click-count increment (no read-then-write race condition).
    # ------------------------------------------------------------------
    await db.execute(
        update(Link)
        .where(Link.id == link.id)
        .values(click_count=Link.click_count + 1)
    )

    # ------------------------------------------------------------------
    # Record click event in the same transaction.
    # ------------------------------------------------------------------
    raw_ip = request.client.host if request.client else None
    click = Click(
        link_id=link.id,
        ip_address=_anonymize_ip(raw_ip),
        user_agent=request.headers.get("user-agent"),
        referer=request.headers.get("referer"),
    )
    db.add(click)

    # ------------------------------------------------------------------
    # Choose redirect status code.
    # ------------------------------------------------------------------
    is_temporary = link.expires_at is not None or link.max_clicks is not None
    status_code = 302 if is_temporary else 301

    return RedirectResponse(
        url=link.destination_url,
        status_code=status_code,
    )

