from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.database import get_db
from linkvault.models.click import Click, _REFERER_MAX, _USER_AGENT_MAX
from linkvault.models.link import Link

logger = logging.getLogger(__name__)
router = APIRouter(tags=["redirects"])


def _anonymize_ip(ip: str | None) -> str | None:
    """Anonymize an IP address for privacy before storing it.

    - **IPv4**: zeroes the last octet  (e.g. ``1.2.3.4`` → ``1.2.3.0``).
    - **IPv6**: zeroes the last 80 bits (last 5 groups) so that the /48
      network prefix is retained but the individual host is not identifiable.
      SEC-13: previously IPv6 addresses were stored verbatim, which could
      uniquely identify a user.

    Returns ``None`` if *ip* is ``None`` or cannot be parsed.
    """
    if ip is None:
        return None

    # IPv4: zero the last octet
    parts = ip.split(".")
    if len(parts) == 4:
        parts[-1] = "0"
        return ".".join(parts)

    # IPv6: attempt to parse and zero the last 80 bits
    try:
        import ipaddress
        addr = ipaddress.IPv6Address(ip)
        # Keep the top 48 bits (first 3 groups), zero the rest
        packed = addr.packed  # 16 bytes
        anonymized = packed[:6] + b"\x00" * 10
        return str(ipaddress.IPv6Address(anonymized))
    except ValueError:
        pass

    # Unrecognised format — do not store
    return None


def _utc_now() -> datetime:
    """Return the current UTC time as a naive datetime (for DB storage).

    SEC-14: replaces the deprecated ``datetime.utcnow()`` call.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.get("/{slug}", response_model=None)
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
        logger.warning(
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
    # SEC-14: use timezone-aware helper instead of datetime.utcnow()
    # ------------------------------------------------------------------
    now = _utc_now()

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
    # SEC-07: truncate user_agent and referer to their column limits so that
    #         oversized headers cannot cause storage-exhaustion or DB errors.
    # SEC-13: anonymize IPv6 addresses (previously stored verbatim).
    # ------------------------------------------------------------------
    raw_ip = request.client.host if request.client else None
    raw_ua = request.headers.get("user-agent")
    raw_ref = request.headers.get("referer")

    click = Click(
        link_id=link.id,
        ip_address=_anonymize_ip(raw_ip),
        # SEC-07: truncate to column limits
        user_agent=raw_ua[:_USER_AGENT_MAX] if raw_ua else None,
        referer=raw_ref[:_REFERER_MAX] if raw_ref else None,
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

