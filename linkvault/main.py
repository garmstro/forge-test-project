from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from linkvault.api.analytics import router as analytics_router
from linkvault.api.health import router as health_router
from linkvault.api.links import router as links_router
from linkvault.api.redirects import router as redirects_router
from linkvault.api.users import router as users_router
from linkvault.config import settings
from linkvault.services.cleanup import create_scheduler, schedule_cleanup_job

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Validate settings, then start/stop the cleanup scheduler with the app."""
    # Fail loudly before serving anything if required settings are missing.
    settings.validate_required()

    application.state.scheduler = None
    if settings.SCHEDULER_ENABLED:
        scheduler = create_scheduler(settings.SCHEDULER_DB_URL)
        scheduler.start()
        schedule_cleanup_job(scheduler)
        application.state.scheduler = scheduler
        logger.info("Cleanup scheduler started (job store: %s)", settings.SCHEDULER_DB_URL)
    else:
        logger.warning("SCHEDULER_ENABLED is false; cleanup scheduler not started")

    try:
        yield
    finally:
        scheduler = application.state.scheduler
        if scheduler is not None and scheduler.running:
            scheduler.shutdown()
            logger.info("Cleanup scheduler shut down")
        application.state.scheduler = None


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
    # Routers
    # ------------------------------------------------------------------
    application.include_router(health_router)  # before /{slug} so it isn't treated as a slug
    application.include_router(users_router)
    application.include_router(links_router)
    application.include_router(analytics_router)
    application.include_router(redirects_router)  # must be last (catches /{slug})

    return application


app = create_app()
