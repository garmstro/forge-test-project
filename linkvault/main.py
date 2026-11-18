from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from linkvault.api.analytics import router as analytics_router
from linkvault.api.links import router as links_router
from linkvault.api.redirects import router as redirects_router
from linkvault.api.users import router as users_router
from linkvault.config import settings
from linkvault.services.cleanup import create_scheduler, shutdown_scheduler, start_scheduler

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Start the background cleanup scheduler on startup, stop it on shutdown."""
    scheduler = None
    if settings.SCHEDULER_ENABLED:
        scheduler = create_scheduler()
        start_scheduler(scheduler)
    application.state.scheduler = scheduler
    try:
        yield
    finally:
        if scheduler is not None:
            shutdown_scheduler(scheduler)
        application.state.scheduler = None


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
    # Health endpoint (registered before the /{slug} catch-all router)
    # ------------------------------------------------------------------
    @application.get("/health", tags=["health"])
    async def health(request: Request) -> dict[str, str]:
        scheduler = getattr(request.app.state, "scheduler", None)
        if scheduler is None:
            scheduler_status = "not_started"
        elif scheduler.running:
            scheduler_status = "running"
        else:
            scheduler_status = "stopped"
        return {
            "status": "ok",
            "db": "ok",
            "scheduler": scheduler_status,
            "version": settings.VERSION,
        }

    # ------------------------------------------------------------------
    # Routers
    # ------------------------------------------------------------------
    application.include_router(users_router)
    application.include_router(links_router)
    application.include_router(analytics_router)
    application.include_router(redirects_router)  # must be last (catches /{slug})

    return application


app = create_app()

