"""
Database engine and session factory.

Two engines exist in this project and mixing them is the trap. The API process is
asyncio and uses asyncpg through `create_async_engine`. Celery workers are **not**
asyncio (CLAUDE.md §8) and use the sync engine with psycopg — importing the async
session into a task produces event-loop errors that look like connection bugs and cost
an evening. The sync half lands with v1.7; this module is the async half only, and says
so in its name.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def build_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """
    Create the async engine.

    `pool_pre_ping` because a pooled connection can be closed by the database, a proxy
    or a container restart while it sits idle, and the failure surfaces as a confusing
    error on the *next* query rather than at the moment it was dropped. One round trip
    per checkout is cheap insurance against that.
    """
    return create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=5,
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """
    Build the session factory.

    `expire_on_commit=False` so an object stays readable after its transaction commits.
    The default would re-fetch attributes on first access after commit, which in async
    code means an implicit IO at an arbitrary point — including inside a place where the
    session may already be gone.
    """
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def transaction(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """
    One unit of work: a session and a transaction that commits together or not at all.

    Every write path in the store goes through this. The explicit context manager is the
    point — a step row, its messages and the run's usage counters must land in one
    transaction, and "commit at the end of the request" is not a strong enough statement
    of that.
    """
    async with factory() as session, session.begin():
        yield session
