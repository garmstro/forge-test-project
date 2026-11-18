from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.config import settings
from linkvault.database import get_db
from linkvault.services.cleanup import scheduler_status

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/health", response_model=None)
async def health(request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    """Report real DB and scheduler state.

    - 200 ``{"status": "ok", "db": "ok", "scheduler": "running", ...}`` when healthy.
    - 503 with ``"status": "error"`` if the DB query fails (``"db": "error"``)
      or the cleanup scheduler is not running (``"scheduler": "stopped"``).
    """
    db_state = "ok"
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:  # any failure to reach the DB is "unhealthy"
        logger.warning("Health check DB query failed: %s", exc)
        db_state = "error"

    sched_state = scheduler_status(getattr(request.app.state, "scheduler", None))

    healthy = db_state == "ok" and sched_state == "running"
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "error",
            "db": db_state,
            "scheduler": sched_state,
            "version": settings.VERSION,
        },
    )
