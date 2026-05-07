"""
Shared pytest fixtures for the LinkVault test suite.

Every test gets a completely fresh in-memory SQLite database so tests
are fully isolated with no shared state.
"""
from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from linkvault.database import Base, get_db
from linkvault.main import app

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture()
async def db_engine():
    """Create a fresh in-memory engine + schema for each test."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        future=True,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture()
async def db_session(db_engine):
    """
    Yields an AsyncSession bound to the test engine.
    Auto-commits after each request via the override so that subsequent
    requests in the same test can see the written data.
    """
    factory = async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )
    session = factory()
    yield session
    await session.close()


@pytest_asyncio.fixture()
async def client(db_session: AsyncSession, db_engine):
    """
    AsyncClient with the DB dependency overridden to use the test engine.

    Each dependency invocation opens a new session so that writes from
    one request are committed and visible to the next.
    """
    factory = async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )

    async def override_get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()

