from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import STATE_PAUSED, STATE_RUNNING
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from linkvault.api.analytics import router as analytics_router
from linkvault.api.links import router as links_router
from linkvault.api.redirects import router as redirects_router
from linkvault.api.users import router as users_router
from linkvault.config import settings
from linkvault.services.cleanup import CLEANUP_JOB_ID, CLEANUP_JOB_REF

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)


def create_scheduler() -> AsyncIOScheduler:
    """Build the APScheduler instance with a persistent SQLite job store.

    The cleanup job is registered with a fixed id and ``replace_existing=True``
    so restarting the app updates the persisted job instead of adding a
    duplicate.
    """
    scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=settings.SCHEDULER_DB_URL)},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
        timezone="UTC",
    )
    scheduler.add_job(
        CLEANUP_JOB_REF,
        trigger="interval",
        minutes=settings.CLEANUP_INTERVAL_MINUTES,
        id=CLEANUP_JOB_ID,
        name="Expired link cleanup",
        replace_existing=True,
    )
    return scheduler


def scheduler_status(scheduler: AsyncIOScheduler | None) -> str:
    """Return ``running``, ``paused`` or ``stopped`` for the health endpoint."""
    if scheduler is None:
        return "stopped"
    if scheduler.state == STATE_RUNNING:
        return "running"
    if scheduler.state == STATE_PAUSED:
        return "paused"
    return "stopped"


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    scheduler = create_scheduler()
    scheduler.start()
    application.state.scheduler = scheduler
    logger.info("Scheduler started (cleanup every %s minutes)", settings.CLEANUP_INTERVAL_MINUTES)
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
            # AsyncIOScheduler defers the real shutdown to the event loop;
            # yield once so it completes (and the job store is closed)
            # before the application finishes stopping.
            await asyncio.sleep(0)
        logger.info("Scheduler stopped")


def create_app() -> FastAPI:
    application = FastAPI(
        title="LinkVault",
        version=settings.VERSION,
        description="A production-grade URL shortening and analytics platform.",
        lifespan=lifespan,
    )
    application.state.scheduler = None

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
    # Health endpoint (registered before the /{slug} catch-all)
    # ------------------------------------------------------------------
    @application.get("/health", tags=["health"])
    async def health(request: Request) -> dict[str, str]:
        return {
            "status": "ok",
            "db": "ok",
            "scheduler": scheduler_status(request.app.state.scheduler),
            "version": settings.VERSION,
        }

    application.include_router(redirects_router)  # must be last (catches /{slug})

    return application


app = create_app()

