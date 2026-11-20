from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.api.analytics import router as analytics_router
from linkvault.api.links import router as links_router
from linkvault.api.redirects import router as redirects_router
from linkvault.api.users import router as users_router
from linkvault.config import settings
from linkvault.database import get_db
from linkvault.services.cleanup import (
    is_scheduler_running,
    shutdown_scheduler,
    start_scheduler,
)

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Start the background scheduler on startup; stop it on shutdown."""
    start_scheduler()
    try:
        yield
    finally:
        shutdown_scheduler()


def create_app() -> FastAPI:
    application = FastAPI(
        title="LinkVault",
        version=settings.VERSION,
        description="A production-grade URL shortening and analytics platform.",
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------
    # Custom error envelope: {"error": "...", "detail": "..."}
    # ------------------------------------------------------------------
    @application.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first_error = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(loc) for loc in first_error.get("loc", []))
        msg = first_error.get("msg", "Validation error.")
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": "validation_error", "detail": f"{field}: {msg}"},
        )

    @application.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "internal_server_error", "detail": "An unexpected error occurred."},
        )

    # ------------------------------------------------------------------
    # Health endpoint (registered before the /{slug} catch-all)
    # ------------------------------------------------------------------
    @application.get("/health", tags=["health"])
    async def health(db: AsyncSession = Depends(get_db)) -> JSONResponse:
        try:
            await db.execute(text("SELECT 1"))
            db_ok = True
        except Exception as exc:  # noqa: BLE001 — any DB failure means unhealthy
            logger.warning("Health check database probe failed: %s", exc)
            db_ok = False
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
        body = {
            "status": "ok" if db_ok else "error",
            "db": "ok" if db_ok else "error",
            "scheduler": "running" if is_scheduler_running() else "not_running",
            "version": settings.VERSION,
        }
        code = status.HTTP_200_OK if db_ok else status.HTTP_503_SERVICE_UNAVAILABLE
        return JSONResponse(status_code=code, content=body)

    # ------------------------------------------------------------------
    # Routers
    # ------------------------------------------------------------------
    application.include_router(users_router)
    application.include_router(links_router)
    application.include_router(analytics_router)
    application.include_router(redirects_router)  # must be last (catches /{slug})

    return application


app = create_app()

