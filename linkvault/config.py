from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./linkvault.db"

    # Security — must be set in .env; app fails loudly on startup if missing or empty
    SECRET_KEY: str = ""

    # Application
    BASE_URL: str = "http://localhost:8000"
    LOG_LEVEL: str = "INFO"

    # APScheduler persistent job store
    SCHEDULER_DB_URL: str = "sqlite:///./scheduler.db"

    # Application version
    VERSION: str = "0.1.0"

    # ---------------------------------------------------------------------------
    # Rate limiting
    # ---------------------------------------------------------------------------
    # Limits follow the slowapi / limits library format: "N/period"
    # where period is one of: second, minute, hour, day.
    # Set to an empty string to disable a particular tier.

    # Applied to all authenticated API endpoints (links, analytics, users/token).
    # Keyed by authenticated user ID when a Bearer token is present, otherwise IP.
    RATE_LIMIT_API: str = "200/minute"

    # Applied to the public redirect endpoint (GET /{slug}).
    # Keyed by IP address only (no auth on that path).
    # Set deliberately high to avoid impacting the hot path in normal use.
    RATE_LIMIT_REDIRECT: str = "300/minute"

    def validate_required(self) -> None:
        """Call on startup to fail loudly if critical settings are missing."""
        if not self.SECRET_KEY:
            raise RuntimeError(
                "SECRET_KEY is not set. "
                "Copy .env.example to .env and set a real secret key."
            )


settings = Settings()

