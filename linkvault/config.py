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
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_REGISTER_PER_HOUR: int = 5  # per IP
    RATE_LIMIT_TOKEN_PER_MINUTE: int = 10  # per IP
    RATE_LIMIT_CREATE_LINK_PER_HOUR: int = 100  # per user
    RATE_LIMIT_REDIRECT_PER_MINUTE: int = 1000  # per IP (high threshold for public endpoint)
    RATE_LIMIT_REDIRECT_ENABLED: bool = False  # disabled by default for public endpoint

    def validate_required(self) -> None:
        """Call on startup to fail loudly if critical settings are missing."""
        if not self.SECRET_KEY:
            raise RuntimeError(
                "SECRET_KEY is not set. "
                "Copy .env.example to .env and set a real secret key."
            )


settings = Settings()

