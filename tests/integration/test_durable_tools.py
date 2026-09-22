"""
Durable tools against a real Postgres: at-least-once delivery, effectively-once effects.

The unit tests prove the *control flow* with an in-memory ledger. These prove the part
only Postgres can provide — that `UNIQUE(idempotency_key)` is what arbitrates a race
between two workers holding the same task, and that the winner's row is what the loser
reads instead of executing.

A duplicate delivery is simulated by calling the task's work twice. That is not an
approximation: a redelivered Celery task *is* the same function called again with the
same arguments in a different process, and the only thing standing between it and a
second side effect is the row.

The broker itself is not exercised here. Whether Redis redelivers is Celery's
responsibility and is configured in `celery_app.py`; what is ours is that redelivery is
harmless when it happens.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.adapters.db.run_store import PostgresRunStore
from app.adapters.db.tool_ledger import PostgresToolLedger
from app.core.runtime.errors import ToolCrashed
from app.core.runtime.idempotency import InvocationStatus, invocation_key
from app.core.runtime.state import NewRun, RunLimits
from app.core.tools.base import EffectClass, ExecutionMode, ToolSpec
from app.core.tools.execution import execute_invocation
from app.core.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration

TOOL = "durable_write"


class Empty(BaseModel):
    pass


@pytest.fixture
async def ledger(session_factory: async_sessionmaker[AsyncSession]) -> PostgresToolLedger:
    return PostgresToolLedger(session_factory)


@pytest.fixture
async def run_id(store: PostgresRunStore) -> uuid.UUID:
    record = await store.create(
        NewRun(
            agent="researcher",
            input={"query": "q"},
            limits=RunLimits(),
            effort="medium",
            prompt_name="agent",
            prompt_version=1,
            model="claude-opus-5",
        )
    )
    return record.id


def registry_for(
    executions: list[str],
    *,
    effect_class: EffectClass = EffectClass.IDEMPOTENT_WRITE,
    explode: bool = False,
) -> ToolRegistry:
    """A registry with one durable tool that counts its executions."""

    async def handler(payload: Empty) -> str:
        executions.append("ran")
        if explode:
            raise RuntimeError("the tool is broken")
        return f"execution #{len(executions)}"

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name=TOOL,
            description="Writes something we can count.",
            input_model=Empty,
            handler=handler,
            effect_class=effect_class,
            execution=ExecutionMode.DURABLE,
        )
    )
    return registry


async def test_a_redelivered_task_reads_the_row_instead_of_running_again(
    ledger: PostgresToolLedger, run_id: uuid.UUID
) -> None:
    """
    The bullet: `acks_late` makes delivery at-least-once, the ledger makes the effect
    effectively-once. Four deliveries, one side effect.
    """
    executions: list[str] = []
    registry = registry_for(executions)
    key = invocation_key(run_id, 0, TOOL, {})

    outcomes = [
        await execute_invocation(
            registry=registry,
            ledger=ledger,
            run_id=run_id,
            step_idx=0,
            tool_name=TOOL,
            args={},
            key=key,
        )
        for _ in range(4)
    ]

    assert executions == ["ran"]
    assert {o.result for o in outcomes} == {"execution #1"}


async def test_two_workers_racing_one_task_produce_one_execution(
    session_factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> None:
    """
    Redis hands the same task to two workers when the first outruns the visibility
    timeout. They arrive at `begin` together, and the unique constraint is what decides.

    Each worker gets its own ledger instance, because in production they are separate
    processes with separate pools — sharing one would quietly test a single connection
    instead of a race.
    """
    executions: list[str] = []
    registry = registry_for(executions)
    key = invocation_key(run_id, 0, TOOL, {})

    async def deliver() -> None:
        await execute_invocation(
            registry=registry,
            ledger=PostgresToolLedger(session_factory),
            run_id=run_id,
            step_idx=0,
            tool_name=TOOL,
            args={},
            key=key,
        )

    await asyncio.gather(*(deliver() for _ in range(5)))

    assert executions == ["ran"]


async def test_an_unsafe_tool_interrupted_mid_flight_is_marked_for_review(
    ledger: PostgresToolLedger, run_id: uuid.UUID
) -> None:
    """
    The t1-t2 window inside a worker, and the thing the status exists for.

    The first delivery claims the row and dies (simulated by claiming and stopping). The
    redelivery finds PENDING on an UNSAFE tool and must not execute — but it is in a
    process the run cannot see, so refusing is not enough. It has to write the refusal
    where the run will read it.
    """
    executions: list[str] = []
    registry = registry_for(executions, effect_class=EffectClass.UNSAFE)
    key = invocation_key(run_id, 0, TOOL, {})

    await ledger.begin(run_id, 0, TOOL, EffectClass.UNSAFE, key)  # claimed, then killed

    outcome = await execute_invocation(
        registry=registry,
        ledger=ledger,
        run_id=run_id,
        step_idx=0,
        tool_name=TOOL,
        args={},
        key=key,
    )

    assert executions == []
    assert outcome.status is InvocationStatus.NEEDS_REVIEW

    stored = await ledger.lookup(key)
    assert stored is not None
    assert stored.status is InvocationStatus.NEEDS_REVIEW
    assert stored.is_terminal


async def test_a_read_only_tool_interrupted_mid_flight_is_simply_run_again(
    ledger: PostgresToolLedger, run_id: uuid.UUID
) -> None:
    """
    The same window, the other answer. READ_ONLY means replay costs time and nothing
    else, so the redelivery executes rather than stopping the run for a human.
    """
    executions: list[str] = []
    registry = registry_for(executions, effect_class=EffectClass.READ_ONLY)
    key = invocation_key(run_id, 0, TOOL, {})

    await ledger.begin(run_id, 0, TOOL, EffectClass.READ_ONLY, key)

    outcome = await execute_invocation(
        registry=registry,
        ledger=ledger,
        run_id=run_id,
        step_idx=0,
        tool_name=TOOL,
        args={},
        key=key,
    )

    assert executions == ["ran"]
    assert outcome.status is InvocationStatus.SUCCEEDED


async def test_a_crashing_tool_closes_its_row_before_the_exception_escapes(
    ledger: PostgresToolLedger, run_id: uuid.UUID
) -> None:
    """
    A bug must not leave the row PENDING. PENDING is a promise that somebody is still
    working on it, and a waiting run believes that promise until its step budget runs
    out — so a crash that left it would read as patience, forever, on every resume.
    """
    executions: list[str] = []
    registry = registry_for(executions, explode=True)
    key = invocation_key(run_id, 0, TOOL, {})

    with pytest.raises(ToolCrashed):
        await execute_invocation(
            registry=registry,
            ledger=ledger,
            run_id=run_id,
            step_idx=0,
            tool_name=TOOL,
            args={},
            key=key,
        )

    stored = await ledger.lookup(key)
    assert stored is not None
    assert stored.status is InvocationStatus.FAILED
    assert stored.is_error


async def test_lookup_does_not_claim_the_invocation(
    ledger: PostgresToolLedger, engine: AsyncEngine, run_id: uuid.UUID
) -> None:
    """
    The loop polls this hundreds of times while a tool runs. If it wrote anything, the
    waiting itself would be the duplicate dispatch the design is avoiding — so the
    assertion is on the row count, not on the return value.
    """
    key = invocation_key(run_id, 0, TOOL, {})

    assert await ledger.lookup(key) is None
    for _ in range(10):
        assert await ledger.lookup(key) is None

    async with engine.begin() as conn:
        rows = await conn.scalar(text("SELECT count(*) FROM tool_invocations"))
    assert rows == 0
