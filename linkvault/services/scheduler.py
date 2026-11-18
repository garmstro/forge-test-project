"""APScheduler manager for background jobs."""
from __future__ import annotations

import logging
from typing import Callable

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import async_sessionmaker

from linkvault.services.cleanup import cleanup_expired_links

logger = logging.getLogger(__name__)


class SchedulerManager:
    """
    Manages APScheduler for periodic background jobs.
    
    Uses AsyncIOScheduler (compatible with FastAPI's asyncio event loop)
    and persists job state to a SQLite database for resilience across restarts.
    """
    
    def __init__(
        self,
        db_factory: async_sessionmaker,
        scheduler_db_url: str,
        logger_instance: logging.Logger | None = None,
    ):
        """
        Initialize the scheduler manager.
        
        Args:
            db_factory: Async session factory for database operations.
            scheduler_db_url: SQLAlchemy URL for the APScheduler job store.
            logger_instance: Optional logger instance (defaults to module logger).
        """
        self.db_factory = db_factory
        self.scheduler_db_url = scheduler_db_url
        self.logger = logger_instance or logger
        self.scheduler: AsyncIOScheduler | None = None
    
    def start(self) -> None:
        """
        Initialize and start the scheduler.
        
        Schedules the cleanup_expired_links job to run every 15 minutes.
        Handles startup errors gracefully by logging warnings without crashing.
        """
        try:
            # Configure APScheduler with SQLAlchemy job store
            jobstores = {
                "default": SQLAlchemyJobStore(url=self.scheduler_db_url)
            }
            
            self.scheduler = AsyncIOScheduler(jobstores=jobstores)
            
            # Schedule cleanup job every 15 minutes (900 seconds)
            self.scheduler.add_job(
                self._run_cleanup_job,
                trigger="interval",
                seconds=900,
                id="cleanup_expired_links",
                replace_existing=True,
                max_instances=1,  # Prevent concurrent runs
            )
            
            self.scheduler.start()
            self.logger.info("Scheduler started successfully")
            
        except Exception as e:
            self.logger.warning(
                f"Failed to start scheduler: {e}. Background jobs will not run.",
                exc_info=True,
            )
    
    def stop(self) -> None:
        """Gracefully shut down the scheduler."""
        if self.scheduler and self.scheduler.running:
            try:
                self.scheduler.shutdown(wait=True)
                self.logger.info("Scheduler stopped")
            except Exception as e:
                self.logger.warning(f"Error stopping scheduler: {e}", exc_info=True)
    
    def is_running(self) -> bool:
        """Check if the scheduler is currently running."""
        return self.scheduler is not None and self.scheduler.running
    
    async def _run_cleanup_job(self) -> None:
        """
        Wrapper for the cleanup job that handles DB session creation and errors.
        
        This wrapper ensures each job run gets its own database session and
        that exceptions are caught and logged without crashing the scheduler.
        """
        try:
            async with self.db_factory() as session:
                await cleanup_expired_links(session)
        except Exception:
            self.logger.exception("Cleanup job failed")
            # Don't re-raise — APScheduler will log and continue to next interval


__all__ = ["SchedulerManager"]
