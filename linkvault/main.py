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
from linkvault.database import AsyncSessionLocal
from linkvault.services.scheduler import SchedulerManager

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)

# Global scheduler manager instance
scheduler_manager: SchedulerManager | None = None


def create_app() -> FastAPI:
    application = FastAPI(
        title="LinkVault",
        version=settings.VERSION,
        description="A production-grade URL shortening and analytics platform.",
    )

    # ------------------------------------------------------------------
    # Startup event: validate settings and start scheduler
    # ------------------------------------------------------------------
    @application.on_event("startup")
    async def startup_event() -> None:
        """Validate required settings and start the scheduler."""
        global scheduler_manager
        
        try:
            # Validate required settings first
            settings.validate_required()
            
            # Initialize and start the scheduler
            scheduler_manager = SchedulerManager(
                db_factory=AsyncSessionLocal,
                scheduler_db_url=settings.SCHEDULER_DB_URL,
                logger_instance=logger,
            )
            scheduler_manager.start()
            logger.info("Scheduler started")
            
        except Exception as e:
            logger.error(f"Error during startup: {e}", exc_info=True)
            # Don't crash the app if scheduler fails to start

    # ------------------------------------------------------------------
    # Shutdown event: stop scheduler gracefully
    # ------------------------------------------------------------------
    @application.on_event("shutdown")
    async def shutdown_event() -> None:
        """Stop the scheduler gracefully."""
        global scheduler_manager
        
        if scheduler_manager:
            scheduler_manager.stop()
            logger.info("Scheduler stopped")

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
        scheduler_status = "running" if (scheduler_manager and scheduler_manager.is_running()) else "not_started"
        return {
            "status": "ok",
            "db": "ok",
            "scheduler": scheduler_status,
            "version": settings.VERSION,
        }

    return application


app = create_app()

