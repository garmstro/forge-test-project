from __future__ import annotations

import asyncio
import logging

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

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)


async def check_database(session: AsyncSession, timeout: float) -> bool:
    """Run a trivial ``SELECT 1`` probe; return True if the database answered in time.

    Any failure (connection error, timeout, ...) is logged and reported as
    ``False`` so that callers never leak driver details to clients.
    """
    try:
        await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=timeout)
    except asyncio.TimeoutError:
        logger.error("Health check: database did not respond within %.1fs", timeout)
    except Exception:
        logger.exception("Health check: database probe failed")
    else:
        return True

    # Leave the session clean so the get_db dependency can close it safely.
    try:
        await session.rollback()
    except Exception:
        logger.debug("Health check: rollback after failed probe also failed", exc_info=True)
    return False


def scheduler_status(application: FastAPI) -> str:
    """Report the background scheduler's state.

    The cleanup job stores its APScheduler instance on ``app.state.scheduler``.
    Until that is wired up (or when it is disabled) this reports ``not_started``.
    """
    scheduler = getattr(application.state, "scheduler", None)
    if scheduler is None:
        return "not_started"
    return "running" if getattr(scheduler, "running", False) else "stopped"


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
    # Health endpoint (public; registered before the /{slug} catch-all router)
    #
    # Only the database decides the HTTP status: links keep working while the
    # scheduler is down, so a stopped scheduler is reported but never yields 503.
    # ------------------------------------------------------------------
    @application.get("/health", tags=["health"])
    async def health(request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
        db_ok = await check_database(db, settings.HEALTH_DB_TIMEOUT_SECONDS)
        return JSONResponse(
            status_code=status.HTTP_200_OK if db_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "ok" if db_ok else "error",
                "db": "ok" if db_ok else "error",
                "scheduler": scheduler_status(request.app),
                "version": settings.VERSION,
            },
        )

    # ------------------------------------------------------------------
    # Routers
    # ------------------------------------------------------------------
    application.include_router(users_router)
    application.include_router(links_router)
    application.include_router(analytics_router)
    application.include_router(redirects_router)  # must be last (catches /{slug})

    return application


app = create_app()

