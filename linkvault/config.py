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

    # Rate limiting settings
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_AUTH: str = "10/minute"
    RATE_LIMIT_LINKS: str = "60/minute"
    RATE_LIMIT_ANALYTICS: str = "30/minute"
    RATE_LIMIT_REDIRECTS: str = "120/minute"
    RATE_LIMIT_DEFAULT: str = "100/minute"

    def validate_required(self) -> None:
        """Call on startup to fail loudly if critical settings are missing."""
        if not self.SECRET_KEY:
            raise RuntimeError(
                "SECRET_KEY is not set. "
                "Copy .env.example to .env and set a real secret key."
            )


settings = Settings()

