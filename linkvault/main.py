from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from linkvault.api.analytics import router as analytics_router
from linkvault.api.links import router as links_router
from linkvault.api.redirects import router as redirects_router
from linkvault.api.users import router as users_router
from linkvault.config import settings
from linkvault.scheduler import scheduler, start_scheduler, stop_scheduler

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)

# Tracks whether the scheduler started successfully
_scheduler_error: str | None = None


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    """Start the background scheduler on startup; stop it on shutdown."""
    global _scheduler_error
    try:
        start_scheduler()
        _scheduler_error = None
    except Exception as exc:  # pragma: no cover
        logger.warning("Scheduler failed to start: %s", exc)
        _scheduler_error = str(exc)
    try:
        yield
    finally:
        stop_scheduler()


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
    # Health endpoint — registered BEFORE routers so it takes priority
    # over the /{slug} catch-all in the redirects router.
    # ------------------------------------------------------------------
    @application.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        if _scheduler_error is not None:
            scheduler_status = "error"
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
    # Routers (redirects last — it has a /{slug} catch-all)
    # ------------------------------------------------------------------
    application.include_router(users_router)
    application.include_router(links_router)
    application.include_router(analytics_router)
    application.include_router(redirects_router)  # must be last (catches /{slug})

    return application


app = create_app()
