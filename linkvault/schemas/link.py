from __future__ import annotations

import ipaddress
from datetime import datetime, timezone
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

# SEC-07: maximum length for a destination URL stored in the database.
# Browsers and most HTTP stacks cap URLs at ~2 000 characters; 2 048 is a
# generous but bounded limit that prevents storage-exhaustion attacks.
_MAX_URL_LENGTH = 2048

# SEC-04: private / loopback / link-local network ranges that must not be
# used as redirect destinations (SSRF prevention).
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),      # IPv4 loopback
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("10.0.0.0/8"),        # RFC-1918 private
    ipaddress.ip_network("172.16.0.0/12"),     # RFC-1918 private
    ipaddress.ip_network("192.168.0.0/16"),    # RFC-1918 private
    ipaddress.ip_network("169.254.0.0/16"),    # IPv4 link-local / AWS metadata
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
    ipaddress.ip_network("fc00::/7"),          # IPv6 unique-local
    ipaddress.ip_network("0.0.0.0/8"),         # "This" network
    ipaddress.ip_network("::/128"),            # IPv6 unspecified
]

_BLOCKED_HOSTNAMES = {"localhost"}


def _is_ssrf_url(url: str) -> bool:
    """Return True if *url* targets a private/loopback address (SSRF risk).

    SEC-04: Resolves the hostname to an IP address at validation time and
    rejects any URL whose host falls within a blocked network range.  This
    prevents attackers from using LinkVault as a proxy to reach internal
    services (cloud metadata endpoints, internal APIs, etc.).

    Note: DNS rebinding is a residual risk that cannot be fully mitigated at
    validation time; infrastructure-level controls (egress firewall) are the
    recommended defence-in-depth layer.
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
    except Exception:
        return True  # unparseable → reject

    if not host:
        return True

    # Block by hostname string first (handles "localhost" and variants)
    if host.lower() in _BLOCKED_HOSTNAMES:
        return True

    # Try to parse the host as a literal IP address
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # Not a literal IP — we cannot resolve DNS at validation time without
        # a blocking call, so we allow non-literal hostnames here.  Operators
        # should add egress firewall rules as defence-in-depth.
        return False

    for network in _BLOCKED_NETWORKS:
        if addr in network:
            return True

    return False


def _validate_destination_url(v: str | None) -> str | None:
    """Shared URL validator used by both LinkCreate and LinkUpdate."""
    if v is None:
        return v

    # SEC-07: enforce maximum URL length
    if len(v) > _MAX_URL_LENGTH:
        raise ValueError(f"url must not exceed {_MAX_URL_LENGTH} characters.")

    if not (v.startswith("http://") or v.startswith("https://")):
        raise ValueError("url must start with http:// or https://")

    # SEC-04: block SSRF targets
    if _is_ssrf_url(v):
        raise ValueError(
            "url must not target a private, loopback, or link-local address."
        )

    return v


class LinkCreate(BaseModel):
    url: str
    slug: str | None = None
    expires_at: datetime | None = None
    # SEC-08: max_clicks must be a positive integer (≥ 1).
    # A value of 0 or negative would make the link immediately unreachable,
    # which is almost certainly a client mistake rather than intent.
    max_clicks: int | None = Field(default=None, ge=1)

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        result = _validate_destination_url(v)
        assert result is not None
        return result

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, v: datetime | None) -> datetime | None:
        # SEC-09: reject expiry dates that are already in the past.
        # Creating a link that is immediately expired is almost certainly a
        # client error and would result in a 410 on the very first redirect.
        if v is not None:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            # Normalise aware datetimes to naive UTC for comparison
            compare_v = v.replace(tzinfo=None) if v.tzinfo is not None else v
            if compare_v <= now:
                raise ValueError("expires_at must be a future datetime.")
        return v


class LinkUpdate(BaseModel):
    url: str | None = None
    expires_at: datetime | None = None
    # SEC-08: same positive-integer constraint as LinkCreate
    max_clicks: int | None = Field(default=None, ge=1)

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        return _validate_destination_url(v)

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, v: datetime | None) -> datetime | None:
        # SEC-09: same future-datetime constraint as LinkCreate
        if v is not None:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            compare_v = v.replace(tzinfo=None) if v.tzinfo is not None else v
            if compare_v <= now:
                raise ValueError("expires_at must be a future datetime.")
        return v


class LinkResponse(BaseModel):
    id: str
    user_id: str
    slug: str
    destination_url: str
    expires_at: datetime | None
    max_clicks: int | None
    click_count: int
    created_at: datetime
    deleted_at: datetime | None

    model_config = {"from_attributes": True}


class PaginatedLinksResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[LinkResponse]

