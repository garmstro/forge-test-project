from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from linkvault.api.analytics import router as analytics_router
from linkvault.api.links import router as links_router
from linkvault.api.redirects import router as redirects_router
from linkvault.api.users import router as users_router
from linkvault.config import settings

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    application = FastAPI(
        title="LinkVault",
        version=settings.VERSION,
        description="A production-grade URL shortening and analytics platform.",
    )

    # ------------------------------------------------------------------
    # SEC-12: CORS policy
    # Only allow origins explicitly listed in CORS_ALLOWED_ORIGINS.
    # An empty list means no cross-origin requests are permitted.
    # ------------------------------------------------------------------
    allowed_origins = (
        [o.strip() for o in settings.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]
        if settings.CORS_ALLOWED_ORIGINS
        else []
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # ------------------------------------------------------------------
    # SEC-11: Security response headers middleware
    # These headers are added to every response regardless of route.
    # ------------------------------------------------------------------
    @application.middleware("http")
    async def add_security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        # Prevent MIME-type sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Deny framing to mitigate clickjacking
        response.headers["X-Frame-Options"] = "DENY"
        # Disable legacy XSS filter (modern browsers ignore it; old ones may
        # introduce vulnerabilities when it is enabled)
        response.headers["X-XSS-Protection"] = "0"
        # Restrict Referer information sent to third-party destinations
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Minimal Content-Security-Policy for an API (no HTML served)
        response.headers["Content-Security-Policy"] = "default-src 'none'"
        # Instruct browsers to only connect over HTTPS for the next year
        # (only meaningful when the app is served over TLS)
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        # Opt out of FLoC / Topics API
        response.headers["Permissions-Policy"] = "interest-cohort=()"
        return response

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
            "scheduler": "not_started",
            "version": settings.VERSION,
        }

    return application


app = create_app()
