"""
Tests for GET /health and startup settings validation.

Covers:
- Healthy server returns 200 with db ok, scheduler running, version 0.1.0
- /health is routed to the health endpoint, not the /{slug} redirect
- Unreachable DB returns 503 with db error
- Stopped scheduler returns 503 with scheduler stopped
- Startup refuses to proceed with an empty SECRET_KEY
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from linkvault.config import settings
from linkvault.database import get_db
from linkvault.main import app, lifespan
from linkvault.models.link import Link
from linkvault.models.user import User


@pytest_asyncio.fixture()
async def running_scheduler():
    """A real, running in-memory scheduler attached to app.state."""
    scheduler = AsyncIOScheduler(jobstores={"default": MemoryJobStore()})
    scheduler.start()
    app.state.scheduler = scheduler
    yield scheduler
    if scheduler.running:
        scheduler.shutdown(wait=False)
        await asyncio.sleep(0)
    app.state.scheduler = None


@pytest.mark.asyncio
async def test_health_ok(client: AsyncClient, running_scheduler) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "db": "ok",
        "scheduler": "running",
        "version": "0.1.0",
    }


@pytest.mark.asyncio
async def test_health_is_not_treated_as_slug(
    client: AsyncClient, running_scheduler, session_factory
) -> None:
    """/health must hit the health route, never the /{slug} catch-all."""
    # "health" is a reserved slug, so insert a link directly, bypassing API
    # validation. If /{slug} were matched first, this request would redirect.
    async with session_factory() as session:
        user = User(email="h@example.com", password_hash="x", api_key_hash="h")
        session.add(user)
        await session.flush()
        session.add(Link(user_id=user.id, slug="health", destination_url="https://other.example/"))
        await session.commit()

    resp = await client.get("/health", follow_redirects=False)
    assert resp.status_code == 200
    assert "location" not in resp.headers
    assert resp.json()["version"] == "0.1.0"


@pytest.mark.asyncio
async def test_health_db_unreachable_returns_503(
    client: AsyncClient, running_scheduler, tmp_path: Path
) -> None:
    # A SQLite file inside a directory that doesn't exist cannot be opened.
    bad_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'missing-dir' / 'nope.db'}"
    )
    bad_factory = async_sessionmaker(bind=bad_engine, class_=AsyncSession)

    async def broken_get_db():
        async with bad_factory() as session:
            yield session

    app.dependency_overrides[get_db] = broken_get_db
    try:
        resp = await client.get("/health")
    finally:
        await bad_engine.dispose()

    assert resp.status_code == 503
    body = resp.json()
    assert body["db"] == "error"
    assert body["status"] == "error"
    assert body["scheduler"] == "running"


@pytest.mark.asyncio
async def test_health_scheduler_not_started_returns_503(client: AsyncClient) -> None:
    app.state.scheduler = None
    resp = await client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["scheduler"] == "stopped"
    assert body["db"] == "ok"
    assert body["status"] == "error"


@pytest.mark.asyncio
async def test_health_scheduler_stopped_returns_503(
    client: AsyncClient, running_scheduler
) -> None:
    running_scheduler.shutdown(wait=False)
    await asyncio.sleep(0)  # AsyncIOScheduler shuts down on the next loop tick

    resp = await client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["scheduler"] == "stopped"


@pytest.mark.asyncio
async def test_health_scheduler_paused_returns_503(
    client: AsyncClient, running_scheduler
) -> None:
    running_scheduler.pause()
    resp = await client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["scheduler"] == "stopped"


@pytest.mark.asyncio
async def test_startup_fails_without_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SECRET_KEY", "")
    with pytest.raises(RuntimeError, match="SECRET_KEY is not set"):
        async with lifespan(app):
            pass  # pragma: no cover - must not be reached
