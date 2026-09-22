"""
The async resources a worker process owns, and the loop they live on (D-024).

CLAUDE.md §8 warns that Celery is not asyncio, and the warning is real — but it is about
a specific bug: using the API's async engine inside a task, where the engine was built
before `fork` or is bound to an event loop that is no longer running. Both are fatal and
both look like connection errors.

What this module does instead is give each worker **process** its own event loop and its
own engine, created inside that process, living as long as it does. Nothing is inherited
across the fork and nothing outlives its loop, so neither half of the trap applies. The
payoff is that the worker uses the same `PostgresToolLedger` the API does, rather than a
sync duplicate of the most subtle SQL in the project kept in step by hand.

One loop per process, not one per task: `asyncio.run` in each task body would bind a new
connection pool to a loop it then destroys, so every task would pay a fresh connect and
the pool would never be a pool.

Initialisation is lazy as well as signal-driven. The `worker_process_init` signal is what
fires under the prefork pool we actually run, but it does not fire under `--pool=solo`,
and it does not fire at all when a test calls the task function directly — which is
exactly how the task's logic is unit-tested.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from celery.signals import worker_process_init, worker_process_shutdown
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.clock import SystemClock
from app.adapters.db.session import build_engine, build_session_factory
from app.adapters.db.tool_ledger import PostgresToolLedger
from app.core.tools.builtin import build_default_registry
from app.core.tools.registry import ToolRegistry
from app.settings import Settings, get_settings


@dataclass(frozen=True, slots=True)
class WorkerResources:
    """Everything a task needs, built once per process."""

    loop: asyncio.AbstractEventLoop
    engine: AsyncEngine
    ledger: PostgresToolLedger
    registry: ToolRegistry
    settings: Settings


_resources: WorkerResources | None = None


def resources() -> WorkerResources:
    """
    Return this process's resources, building them on first use.

    Not a `functools.lru_cache`, because the teardown path has to be able to clear it and
    a cached function cannot be selectively invalidated by the signal that owns its
    lifetime.
    """
    global _resources
    if _resources is None:
        _resources = _build()
    return _resources


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """
    Run one coroutine to completion on this process's loop, and return its result.

    The bridge between Celery's synchronous task protocol and everything else in this
    codebase being async. `run_until_complete` rather than `asyncio.run` so the loop — and
    therefore the connection pool bound to it — survives to the next task.
    """
    return resources().loop.run_until_complete(coro)


def _build() -> WorkerResources:
    """Create the loop, the engine and the ledger, in this process."""
    settings = get_settings()

    loop = asyncio.new_event_loop()
    # Set as *the* loop for this process, so anything that reaches for the running or
    # current loop during a task — a library building a lock, say — finds this one rather
    # than creating a second that nothing drives.
    asyncio.set_event_loop(loop)

    engine = build_engine(settings.database_url)
    ledger = PostgresToolLedger(build_session_factory(engine))

    # The same registry the API builds (`build_default_registry`), so a tool name
    # dispatched from one process resolves to the same handler in the other.
    registry = build_default_registry(SystemClock())

    return WorkerResources(
        loop=loop, engine=engine, ledger=ledger, registry=registry, settings=settings
    )


# Both signal decorators are ignored below: celery.signals is untyped, and relaxing
# `disallow_untyped_decorators` for the whole package would hide the next one.
@worker_process_init.connect  # type: ignore[untyped-decorator]
def _open_resources(**_kwargs: object) -> None:
    """
    Build this child process's resources after the fork.

    After, never before: an asyncpg pool inherited across `fork` shares sockets between
    parent and child, and two processes reading the same connection is a corruption bug
    rather than a connection bug — which is most of why it is so confusing to debug.
    """
    resources()


@worker_process_shutdown.connect  # type: ignore[untyped-decorator]
def _close_resources(**_kwargs: object) -> None:
    """Dispose the pool and close the loop, so a restarting worker leaves nothing behind."""
    global _resources
    if _resources is None:
        return

    current = _resources
    _resources = None
    try:
        # Closes every pooled connection. Without it a restarted worker leaks its
        # sockets until Postgres times them out, and `max_connections` is finite.
        current.loop.run_until_complete(current.engine.dispose())
    finally:
        current.loop.close()
