from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

# SEC-16: minimum acceptable length for SECRET_KEY.
# A key shorter than 32 characters provides insufficient entropy for HMAC-SHA256.
_SECRET_KEY_MIN_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./linkvault.db"

    # Security — must be set in .env; app fails loudly on startup if missing,
    # too short, or left as the example placeholder value.
    # SEC-10: SECRET_KEY is used as the HMAC key when hashing API keys, so it
    # must be a strong, random secret that is NOT stored in the database.
    SECRET_KEY: str = ""

    # Application
    BASE_URL: str = "http://localhost:8000"
    LOG_LEVEL: str = "INFO"

    # APScheduler persistent job store
    SCHEDULER_DB_URL: str = "sqlite:///./scheduler.db"

    # Application version
    VERSION: str = "0.1.0"

    # SEC-12: CORS — comma-separated list of allowed origins.
    # Default is empty (no cross-origin requests allowed).
    # Set to "*" only in development; always use explicit origins in production.
    CORS_ALLOWED_ORIGINS: str = ""

    def validate_required(self) -> None:
        """Call on startup to fail loudly if critical settings are missing or weak."""
        if not self.SECRET_KEY:
            raise RuntimeError(
                "SECRET_KEY is not set. "
                "Copy .env.example to .env and set a real secret key."
            )
        # SEC-16: reject the example placeholder value
        if self.SECRET_KEY.lower().startswith("changeme"):
            raise RuntimeError(
                "SECRET_KEY is still set to the example placeholder value. "
                "Generate a strong random key (e.g. `python -c \"import secrets; "
                "print(secrets.token_hex(32))\"`) and set it in .env."
            )
        # SEC-16: enforce minimum key length
        if len(self.SECRET_KEY) < _SECRET_KEY_MIN_LENGTH:
            raise RuntimeError(
                f"SECRET_KEY must be at least {_SECRET_KEY_MIN_LENGTH} characters long. "
                "Generate a strong random key and set it in .env."
            )


settings = Settings()

