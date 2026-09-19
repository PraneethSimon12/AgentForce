"""
Fixtures for tests that need a real Postgres.

`make up` (or `docker compose up -d postgres`) must be running, and the migrations must
be applied. These are integration tests precisely because the behaviour under test —
transaction atomicity, a unique constraint firing, `SKIP LOCKED` — is behaviour Postgres
provides. Faking it would test the fake.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.adapters.db.run_store import PostgresRunStore
from app.adapters.db.session import build_engine, build_session_factory
from app.settings import Settings


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """
    A fresh engine per test.

    Function-scoped on purpose. An asyncpg connection pool is bound to the event loop
    that created it, and a session-scoped engine shared across tests that each get their
    own loop produces "attached to a different loop" errors that look like connection
    bugs. The cost is a few milliseconds per test; the alternative is an afternoon.
    """
    eng = build_engine(Settings().database_url)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return build_session_factory(engine)


@pytest.fixture(autouse=True)
async def clean_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    """
    Empty the run tables before each test.

    TRUNCATE ... CASCADE rather than DELETE: it is faster, it resets nothing we depend
    on, and CASCADE is required because run_steps and run_messages reference runs.
    Running it *before* rather than after means a failed test leaves its rows behind for
    inspection.
    """
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE runs, run_steps, run_messages CASCADE"))
    yield


@pytest.fixture
async def store(session_factory: async_sessionmaker[AsyncSession]) -> PostgresRunStore:
    return PostgresRunStore(session_factory)
