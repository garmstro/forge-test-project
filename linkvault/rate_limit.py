"""Rate limiting configuration and utilities for LinkVault."""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

# Initialize the limiter with a key function that uses the client's IP address
limiter = Limiter(key_func=get_remote_address)

