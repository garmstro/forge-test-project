from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from linkvault.api.analytics import router as analytics_router
from linkvault.api.links import router as links_router
from linkvault.api.redirects import router as redirects_router
from linkvault.api.users import router as users_router
from linkvault.config import settings
from linkvault.services.scheduler import get_scheduler, init_scheduler, shutdown_scheduler

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    application = FastAPI(
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
    application.include_router(redirects_router)  # must be last (catches /{slug})

    # ------------------------------------------------------------------
    # Startup and shutdown events
    # ------------------------------------------------------------------
    @application.on_event("startup")
    async def startup_event() -> None:
        """Initialize scheduler on app startup."""
        try:
            await init_scheduler()
            logger.info("Scheduler initialized successfully")
        except Exception as e:
            logger.exception("Failed to initialize scheduler: %s", e)
            raise

    @application.on_event("shutdown")
    async def shutdown_event() -> None:
        """Shutdown scheduler on app shutdown."""
        try:
            await shutdown_scheduler()
            logger.info("Scheduler shut down successfully")
        except Exception as e:
            logger.exception("Error during scheduler shutdown: %s", e)

    # ------------------------------------------------------------------
    # Health endpoint
    # ------------------------------------------------------------------
    @application.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        scheduler = get_scheduler()
        scheduler_status = "running" if scheduler is not None and scheduler.running else "not_started"
        return {
            "status": "ok",
            "db": "ok",
            "scheduler": scheduler_status,
            "version": settings.VERSION,
        }

    return application


app = create_app()
