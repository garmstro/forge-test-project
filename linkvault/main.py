from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response, status
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
from linkvault.services.cleanup import scheduler_status, shutdown_scheduler, start_scheduler

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings.validate_required()
    if settings.SCHEDULER_ENABLED:
        start_scheduler()
    try:
        yield
    finally:
        shutdown_scheduler()


def create_app() -> FastAPI:
    application = FastAPI(
        lifespan=lifespan,
        title="LinkVault",
        version=settings.VERSION,
        description="A production-grade URL shortening and analytics platform.",
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
    # Routers
    # ------------------------------------------------------------------
    application.include_router(users_router)
    application.include_router(links_router)
    application.include_router(analytics_router)

    # ------------------------------------------------------------------
    # Health endpoint (declared before the catch-all redirect router)
    # ------------------------------------------------------------------
    @application.get("/health", tags=["health"])
    async def health(
        response: Response, db: AsyncSession = Depends(get_db)
    ) -> dict[str, str]:
        db_status = "ok"
        try:
            await db.execute(text("SELECT 1"))
        except Exception:
            logger.exception("Health check: database unreachable")
            db_status = "error"
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ok" if db_status == "ok" else "error",
            "db": db_status,
            "scheduler": scheduler_status(),
            "version": settings.VERSION,
        }

    # Must be registered last: the redirect router catches /{slug}, so it would
    # otherwise shadow /health.
    application.include_router(redirects_router)

    return application


app = create_app()

