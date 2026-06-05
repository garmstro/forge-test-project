from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from linkvault.api.analytics import router as analytics_router
from linkvault.api.links import router as links_router
from linkvault.api.redirects import router as redirects_router
from linkvault.api.users import router as users_router
from linkvault.config import settings
from linkvault.services.cleanup import expire_links

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler: BackgroundScheduler | None = None


def create_app() -> FastAPI:
    application = FastAPI(
        title="LinkVault",
        version=settings.VERSION,
        description="A production-grade URL shortening and analytics platform.",
    )

    # ------------------------------------------------------------------
    # Custom error envelope: {\"error\": \"...\", \"detail\": \"...\"}
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
        global scheduler
        scheduler_status = "not_started"
        if scheduler is not None and scheduler.running:
            scheduler_status = "running"
        return {
            "status": "ok",
            "db": "ok",
            "scheduler": scheduler_status,
            "version": settings.VERSION,
        }

    # ------------------------------------------------------------------
    # Startup and shutdown events
    # ------------------------------------------------------------------
    @application.on_event("startup")
    async def startup() -> None:
        """Initialize and start the APScheduler background job scheduler."""
        global scheduler
        try:
            # Create job store backed by SQLite
            jobstore = SQLAlchemyJobStore(
                url=settings.SCHEDULER_DB_URL.replace("aiosqlite", "sqlite"),
            )
            scheduler = BackgroundScheduler(
                jobstores={"default": jobstore},
                timezone="UTC",
            )

            # Schedule the cleanup job to run every 15 minutes
            scheduler.add_job(
                expire_links,
                "interval",
                minutes=15,
                id="expire_links",
                name="Expire old links",
                replace_existing=True,
            )

            scheduler.start()
            logger.info("APScheduler started successfully")
        except Exception as exc:
            logger.exception("Failed to start APScheduler: %s", exc)

    @application.on_event("shutdown")
    async def shutdown() -> None:
        """Gracefully shut down the APScheduler scheduler."""
        global scheduler
        if scheduler is not None:
            scheduler.shutdown()
            logger.info("APScheduler shut down successfully")

    return application


app = create_app()
