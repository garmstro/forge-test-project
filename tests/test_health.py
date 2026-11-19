"""Tests for GET /health (AGENTS.md, Item 6)."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from httpx import AsyncClient

from linkvault.database import get_db
from linkvault.main import app
from linkvault.services import cleanup


async def test_health_ok(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["scheduler"] in {"running", "stopped"}
    assert body["version"]


async def test_health_reports_running_scheduler(client: AsyncClient, tmp_path: Any) -> None:
    cleanup.start_scheduler(f"sqlite:///{tmp_path / 'sched.db'}")
    try:
        resp = await client.get("/health")
        assert resp.json()["scheduler"] == "running"
    finally:
        cleanup.shutdown_scheduler()
    resp = await client.get("/health")
    assert resp.json()["scheduler"] == "stopped"


async def test_health_db_unreachable_returns_503(client: AsyncClient) -> None:
    class BrokenSession:
        async def execute(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("db down")

    async def broken_db() -> AsyncGenerator[BrokenSession, None]:
        yield BrokenSession()

    app.dependency_overrides[get_db] = broken_db
    resp = await client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["db"] == "error"
