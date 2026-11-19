from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-untyped]
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from linkvault.api.analytics import router as analytics_router
from linkvault.api.links import router as links_router
from linkvault.api.redirects import router as redirects_router
from linkvault.api.users import router as users_router
from linkvault.config import settings
from linkvault.database import AsyncSessionLocal
from linkvault.services.cleanup import cleanup_expired_links

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)

# Global scheduler state
_scheduler: AsyncIOScheduler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager that starts the scheduler on startup
    and shuts it down on shutdown.
    """
    global _scheduler

    # ---- Startup ----
    _scheduler = AsyncIOScheduler()
    _scheduler.configure(
        jobstores={
            "default": {
                "type": "sqlalchemy",
                "url": settings.SCHEDULER_DB_URL,
            }
        },
        executors={
            "default": {
                "type": "asyncio",
            }
        },
        job_defaults={
            "misfire_grace_time": 60,  # seconds
        },
    )

    # Schedule cleanup job every 15 minutes
    async def cleanup_job() -> None:
        """Wrapper to obtain a database session and run cleanup."""
        async with AsyncSessionLocal() as db:
            await cleanup_expired_links(db)

    _scheduler.add_job(
        cleanup_job,
        trigger=IntervalTrigger(minutes=15),
        id="cleanup_expired_links",
        name="Cleanup expired links",
        replace_existing=True,  # Replace if already scheduled
    )

    _scheduler.start()
    logger.info("APScheduler started. Cleanup job scheduled every 15 minutes.")

    yield

    # ---- Shutdown ----
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=True)
        logger.info("APScheduler shut down.")


def get_scheduler_status() -> str:
    """Return the current scheduler status."""
    if _scheduler is None:
        return "not_started"
    if _scheduler.running:
        return "running"
    return "stopped"


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
    application.include_router(users_router)
    application.include_router(links_router)
    application.include_router(analytics_router)
    application.include_router(redirects_router)  # must be last (catches /{slug})

    # ------------------------------------------------------------------
    # Health endpoint
    # ------------------------------------------------------------------
    @application.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "db": "ok",
            "scheduler": get_scheduler_status(),
            "version": settings.VERSION,
        }

    return application


app = create_app()
