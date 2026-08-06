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

    # Rate Limiting
    # Format: "requests/time_window" (e.g., "100/minute", "1000/hour")
    RATE_LIMIT_REDIRECT: str = "1000/minute"  # Redirect endpoint (public, high volume)
    RATE_LIMIT_API: str = "100/minute"  # General API endpoints (authenticated)
    RATE_LIMIT_AUTH: str = "10/minute"  # Auth endpoints (registration, token)
    RATE_LIMIT_ENABLED: bool = True  # Can be disabled for testing or local development

    def validate_required(self) -> None:
        """Call on startup to fail loudly if critical settings are missing."""
        if not self.SECRET_KEY:
            raise RuntimeError(
                "SECRET_KEY is not set. "
                "Copy .env.example to .env and set a real secret key."
            )


settings = Settings()

