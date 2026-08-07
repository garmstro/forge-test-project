"""
Shared pytest fixtures for the LinkVault test suite.

Every test gets a completely fresh in-memory SQLite database so tests
are fully isolated with no shared state.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from linkvault.database import Base, get_db
from linkvault.main import app
from linkvault.ratelimit import limiter

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(autouse=True)
def disable_rate_limiting():
    """
    Disable rate limiting by default for all tests.
    
    This fixture runs automatically for every test to ensure rate limits
    don't interfere with normal test execution.
    """
    original_enabled = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = original_enabled


@pytest.fixture
def enable_rate_limiting():
    """
    Re-enable rate limiting for tests that need to verify rate limit behavior.
    
    Use this fixture explicitly in tests that need to test rate limiting.
    Also resets the limiter storage between tests.
    """
    limiter.enabled = True
    # Reset the in-memory storage to clear any previous rate limit state
    limiter.reset()
    yield limiter
    limiter.enabled = False


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

