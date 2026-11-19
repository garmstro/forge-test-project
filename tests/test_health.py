"""
Tests for GET /health.

The endpoint probes the database with ``SELECT 1``: 200 when it answers,
503 with ``"db": "error"`` when it does not. The scheduler state is reported
but never changes the status code.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from linkvault.config import settings
from linkvault.database import get_db
from linkvault.main import app

EXPECTED_KEYS = {"status", "db", "scheduler", "version"}


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def broken_db(tmp_path: Path) -> Iterator[None]:
    """Point get_db at a SQLite file inside a directory that does not exist."""
    missing = tmp_path / "does-not-exist" / "linkvault.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{missing}")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    # The ``client`` fixture clears all overrides on teardown.
    app.dependency_overrides[get_db] = override_get_db
    yield


@pytest.fixture()
def fake_scheduler() -> Iterator[SimpleNamespace]:
    """Install a stand-in scheduler on app.state and remove it afterwards."""
    scheduler = SimpleNamespace(running=True)
    previous = getattr(app.state, "scheduler", None)
    app.state.scheduler = scheduler
    yield scheduler
    app.state.scheduler = previous


# ---------------------------------------------------------------------------
# Healthy database
# ---------------------------------------------------------------------------


async def test_health_ok(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == EXPECTED_KEYS
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["version"] == settings.VERSION


async def test_health_is_public(client: AsyncClient) -> None:
    """No Authorization header is needed; a bogus one is ignored."""
    resp = await client.get("/health", headers={"Authorization": "Bearer not-a-key"})
    assert resp.status_code == 200


async def test_health_not_captured_by_slug_router(client: AsyncClient) -> None:
    resp = await client.get("/health", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.json().get("error") is None


# ---------------------------------------------------------------------------
# Broken database
# ---------------------------------------------------------------------------


async def test_health_db_unreachable_returns_503(client: AsyncClient, broken_db: None) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert set(body) == EXPECTED_KEYS
    assert body["status"] == "error"
    assert body["db"] == "error"
    assert body["version"] == settings.VERSION


async def test_health_error_does_not_leak_details(client: AsyncClient, broken_db: None) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 503
    text = resp.text.lower()
    for fragment in ("sqlite", "operationalerror", "unable to open", "traceback", "does-not-exist"):
        assert fragment not in text


async def test_health_db_timeout_returns_503(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hanging database must not hang /health."""

    class HangingSession:
        async def execute(self, *args: Any, **kwargs: Any) -> None:
            await asyncio.sleep(10)

        async def rollback(self) -> None:
            return None

    async def override_get_db() -> AsyncGenerator[Any, None]:
        yield HangingSession()

    monkeypatch.setattr(settings, "HEALTH_DB_TIMEOUT_SECONDS", 0.05)
    app.dependency_overrides[get_db] = override_get_db

    resp = await asyncio.wait_for(client.get("/health"), timeout=5)
    assert resp.status_code == 503
    assert resp.json()["db"] == "error"


# ---------------------------------------------------------------------------
# Scheduler field
# ---------------------------------------------------------------------------


async def test_health_scheduler_not_started_by_default(client: AsyncClient) -> None:
    previous = getattr(app.state, "scheduler", None)
    app.state.scheduler = None
    try:
        resp = await client.get("/health")
    finally:
        app.state.scheduler = previous
    assert resp.json()["scheduler"] == "not_started"


async def test_health_scheduler_running(
    client: AsyncClient, fake_scheduler: SimpleNamespace
) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["scheduler"] == "running"


async def test_health_stopped_scheduler_is_not_a_failure(
    client: AsyncClient, fake_scheduler: SimpleNamespace
) -> None:
    fake_scheduler.running = False
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["scheduler"] == "stopped"
