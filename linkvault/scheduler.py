"""APScheduler setup for LinkVault.

Provides a single ``scheduler`` instance (BackgroundScheduler with a
persistent SQLite job store) and a ``start_scheduler`` / ``stop_scheduler``
pair wired into the FastAPI lifespan.
"""
from __future__ import annotations

import logging

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

from linkvault.config import settings
from linkvault.services.cleanup import cleanup_job

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scheduler singleton
# ---------------------------------------------------------------------------

_job_store = SQLAlchemyJobStore(url=settings.SCHEDULER_DB_URL)

scheduler = BackgroundScheduler(
    jobstores={"default": _job_store},
    job_defaults={"coalesce": True, "max_instances": 1},
)


def start_scheduler() -> None:
    """Add the cleanup job (if not already present) and start the scheduler."""
    if not scheduler.get_job("cleanup"):
        scheduler.add_job(
            cleanup_job,
            trigger="interval",
            minutes=15,
            id="cleanup",
            replace_existing=True,
        )
        logger.info("Registered cleanup job (every 15 minutes).")

    scheduler.start()
    logger.info("APScheduler started.")


def stop_scheduler() -> None:
    """Gracefully shut down the scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped.")
