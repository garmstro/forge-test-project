"""Tests for the expired-link cleanup job (spec item 5)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import linkvault.database
from linkvault.config import settings
from linkvault.main import app
from linkvault.models.link import Link
from linkvault.models.user import User
from linkvault.services import cleanup


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _seed(session: AsyncSession) -> dict[str, str]:
    """Create one user plus links in every state the job cares about."""
    user = User(email="c@example.com", password_hash="x", api_key_hash="y")
    session.add(user)
    await session.flush()
    now = _now()
    links = {
        "expired": Link(user_id=user.id, slug="expired", destination_url="https://a.test",
                        expires_at=now - timedelta(minutes=1)),
        "expired2": Link(user_id=user.id, slug="expired2", destination_url="https://a.test",
                         expires_at=now - timedelta(days=3)),
        "future": Link(user_id=user.id, slug="future", destination_url="https://a.test",
                       expires_at=now + timedelta(days=1)),
        "forever": Link(user_id=user.id, slug="forever", destination_url="https://a.test"),
        "already_deleted": Link(user_id=user.id, slug="gone", destination_url="https://a.test",
                                expires_at=now - timedelta(days=1),
                                deleted_at=now - timedelta(hours=5)),
    }
    session.add_all(links.values())
    await session.commit()
    return {name: link.id for name, link in links.items()}


async def _deleted_at(session: AsyncSession, link_id: str) -> datetime | None:
    return (await session.execute(select(Link.deleted_at).where(Link.id == link_id))).scalar_one()


# ---------------------------------------------------------------------------
# Purge logic
# ---------------------------------------------------------------------------


async def test_purge_soft_deletes_only_expired_active_links(db_session: AsyncSession):
    ids = await _seed(db_session)
    original_deleted = await _deleted_at(db_session, ids["already_deleted"])

    count = await cleanup.run_cleanup(db_session)

    assert count == 2
    assert await _deleted_at(db_session, ids["expired"]) is not None
    assert await _deleted_at(db_session, ids["expired2"]) is not None
    assert await _deleted_at(db_session, ids["future"]) is None
    assert await _deleted_at(db_session, ids["forever"]) is None
    # Already-deleted links keep their original deletion timestamp.
    assert await _deleted_at(db_session, ids["already_deleted"]) == original_deleted


async def test_purge_is_idempotent(db_session: AsyncSession):
    await _seed(db_session)
    assert await cleanup.run_cleanup(db_session) == 2
    assert await cleanup.run_cleanup(db_session) == 0


async def test_cleanup_logs_structured_json(db_session: AsyncSession, caplog):
    await _seed(db_session)
    with caplog.at_level(logging.INFO, logger="linkvault.cleanup"):
        await cleanup.run_cleanup(db_session)

    lines = [r.getMessage() for r in caplog.records if r.name == "linkvault.cleanup"]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert set(payload) == {"event", "links_expired", "timestamp"}
    assert payload["event"] == "cleanup"
    assert payload["links_expired"] == 2
    assert payload["timestamp"].endswith("Z")
    datetime.fromisoformat(payload["timestamp"].removesuffix("Z"))


async def test_cleaned_link_redirect_returns_404(client: AsyncClient, db_session: AsyncSession):
    await _seed(db_session)
    await cleanup.run_cleanup(db_session)
    resp = await client.get("/expired")
    assert resp.status_code == 404


async def test_scheduled_entry_point_uses_app_session_factory(db_engine, monkeypatch):
    factory = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(linkvault.database, "AsyncSessionLocal", factory)
    async with factory() as session:
        await _seed(session)

    assert await cleanup.run_cleanup_job() == 2


# ---------------------------------------------------------------------------
# Scheduler wiring + persistence
# ---------------------------------------------------------------------------


async def test_scheduler_registers_15_minute_job(tmp_path):
    scheduler = cleanup.create_scheduler(f"sqlite:///{tmp_path / 'sched.db'}")
    cleanup.start_scheduler(scheduler)
    try:
        job = scheduler.get_job(cleanup.CLEANUP_JOB_ID)
        assert job is not None
        assert job.trigger.interval == timedelta(minutes=15)
        assert job.func_ref == cleanup.CLEANUP_JOB_FUNC
        assert job.coalesce is True
        assert job.max_instances == 1
    finally:
        cleanup.shutdown_scheduler(scheduler)


async def test_job_survives_restart(tmp_path):
    url = f"sqlite:///{tmp_path / 'sched.db'}"

    first = cleanup.create_scheduler(url)
    cleanup.start_scheduler(first)
    next_run = first.get_job(cleanup.CLEANUP_JOB_ID).next_run_time
    cleanup.shutdown_scheduler(first)

    second = cleanup.create_scheduler(url)
    cleanup.start_scheduler(second)
    try:
        jobs = second.get_jobs()
        assert [j.id for j in jobs] == [cleanup.CLEANUP_JOB_ID]
        # The persisted schedule is kept, not reset.
        assert jobs[0].next_run_time == next_run
    finally:
        cleanup.shutdown_scheduler(second)


async def test_app_lifespan_starts_scheduler_and_health_reports_it(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULER_DB_URL", f"sqlite:///{tmp_path / 'sched.db'}")
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", True)

    async with app.router.lifespan_context(app):
        assert app.state.scheduler.running
        assert app.state.scheduler.get_job(cleanup.CLEANUP_JOB_ID) is not None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/health")
        assert resp.json()["scheduler"] == "running"

    assert app.state.scheduler is None


async def test_scheduler_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    async with app.router.lifespan_context(app):
        assert app.state.scheduler is None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/health")
        assert resp.json()["scheduler"] == "not_started"
