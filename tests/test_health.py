"""
Tests for the health endpoint (Item 6).

Covers:
- 200 with db ok when the database is reachable
- 503 with db error when the database probe fails
- Scheduler status reflects whether the scheduler is actually running
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import OperationalError

from linkvault.config import settings
from linkvault.database import get_db
from linkvault.main import app
from linkvault.services.cleanup import shutdown_scheduler, start_scheduler


@pytest.mark.asyncio
async def test_health_ok(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["version"] == settings.VERSION
    # Lifespan isn't run by the test transport, so the scheduler isn't started.
    assert body["scheduler"] == "not_running"


@pytest.mark.asyncio
async def test_health_db_unreachable_returns_503(client: AsyncClient) -> None:
    class FailingSession:
        async def execute(self, *args: Any, **kwargs: Any) -> None:
            raise OperationalError("SELECT 1", {}, Exception("unable to open database file"))

        async def rollback(self) -> None:
            return None

    async def failing_get_db() -> AsyncGenerator[FailingSession, None]:
        yield FailingSession()

    app.dependency_overrides[get_db] = failing_get_db
    resp = await client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "error"
    assert body["db"] == "error"
    assert body["version"] == settings.VERSION


@pytest.mark.asyncio
async def test_health_reports_running_scheduler(client: AsyncClient, tmp_path: Path) -> None:
    try:
        start_scheduler(f"sqlite:///{tmp_path / 'scheduler.db'}")
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["scheduler"] == "running"
    finally:
        shutdown_scheduler()
    resp = await client.get("/health")
    assert resp.json()["scheduler"] == "not_running"
