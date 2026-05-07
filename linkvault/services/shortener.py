"""Slug generation service — Base58 alphabet, collision-safe."""
from __future__ import annotations

import random
import re

# Base58 alphabet: standard Base58 minus 0, O, I, l
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

SLUG_LENGTH = 6
MAX_RETRIES = 5

RESERVED_SLUGS = {"api", "admin", "health", "docs", "metrics"}
CUSTOM_SLUG_RE = re.compile(r"^[a-zA-Z0-9-]{3,64}$")


def generate_slug(length: int = SLUG_LENGTH) -> str:
    """Return a random Base58 slug of *length* characters."""
    return "".join(random.choices(BASE58_ALPHABET, k=length))


def is_valid_custom_slug(slug: str) -> bool:
    """Return True if *slug* meets custom-slug rules (3–64 chars, alphanumeric + hyphens)."""
    return bool(CUSTOM_SLUG_RE.match(slug))


def is_reserved_slug(slug: str) -> bool:
    """Return True if *slug* is a reserved system word."""
    return slug.lower() in RESERVED_SLUGS

