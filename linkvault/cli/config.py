"""Local credential storage for the LinkVault CLI.

Credentials live in ``~/.linkvault/config.json`` (override the directory with
the ``LINKVAULT_CONFIG_DIR`` environment variable, mainly useful for tests).
The file is written with ``0600`` permissions because it holds an API key.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_API_URL = "http://localhost:8000"
CONFIG_DIR_ENV = "LINKVAULT_CONFIG_DIR"
CONFIG_FILENAME = "config.json"


def config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".linkvault"


def config_path() -> Path:
    return config_dir() / CONFIG_FILENAME


def load_config() -> dict[str, Any]:
    """Return the stored config, or an empty dict if missing or unreadable."""
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(data: dict[str, Any]) -> Path:
    """Write *data* to the config file (mode 0600) and return its path."""
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = config_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(path, 0o600)
    return path


def clear_config() -> bool:
    """Delete the config file. Return True if a file was removed."""
    path = config_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def get_api_key() -> str | None:
    key = load_config().get("api_key")
    return key if isinstance(key, str) and key else None


def resolve_api_url(cli_value: str | None) -> str:
    """Pick the API URL: --api-url / LINKVAULT_API_URL, then stored, then default."""
    if cli_value:
        return cli_value.rstrip("/")
    stored = load_config().get("api_url")
    if isinstance(stored, str) and stored:
        return stored.rstrip("/")
    return DEFAULT_API_URL
