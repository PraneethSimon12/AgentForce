"""
The v1 exit criterion: kill a worker mid-run and prove what survived.

    "An integration test that starts a run with a tool, kills the worker mid-tool,
    restarts it, and asserts: the run completes, the tool executed exactly once, and no
    duplicate step rows exist. This test is the resume bullet."  — plan.md, v1

A real subprocess, really killed. Not an exception, not a cancelled task, not a mocked
failure — those all run some of your code on the way down, and the code a real kill
skips is precisely the code whose absence matters.

Two kills, at two different instants, because they have genuinely different answers and
conflating them is how a resume story gets overstated:

  **t2-t3 — killed after the ledger was completed, before the step was committed.**
  The tool is not run again at all. One attempt, one outcome. This window is closed.

  **t1-t2 — killed mid-tool, before the ledger knew anything.**
  Nothing can know whether the effect happened, so an IDEMPOTENT_WRITE tool is run
  again: *two attempts, one outcome*. That is the honest claim — at-least-once
  execution with effectively-once outcomes — and it only holds because the tool's
  downstream really does deduplicate.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.adapters.clock import SystemClock
from app.adapters.db.run_store import PostgresRunStore
from app.adapters.db.tool_ledger import PostgresToolLedger
from app.adapters.llm.fake_llm import FakeLLM, calls_tool, says
from app.core.runtime.loop import AgentLoop
from app.core.runtime.state import NewRun, RunLimits, RunStatus
from app.core.tools.registry import ToolRegistry
from tests.integration._kill9_tool import CREATE_TABLES, make_side_effect_tool

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLL_INTERVAL = 0.1
POLL_TIMEOUT = 40.0


@pytest.fixture(autouse=True)
async def side_effect_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for statement in CREATE_TABLES:
            await conn.execute(text(statement))
        await conn.execute(text("TRUNCATE test_side_effects, test_side_effect_attempts"))


async def wait_until(engine: AsyncEngine, sql: str, params: dict[str, object]) -> None:
    """
    Poll until a condition holds, so the kill lands where we intend it to.

    Polling the database rather than sleeping a fixed time is what makes this test
    deterministic instead of merely usually-right. A `sleep(2)` would pass on a fast
    machine and kill the worker at the wrong instant on a slow one — producing a test
    that silently checks a different claim than the one it is named after.
    """
    deadline = asyncio.get_running_loop().time() + POLL_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        async with engine.connect() as conn:
            if (await conn.execute(text(sql), params)).scalar_one():
                return
        await asyncio.sleep(POLL_INTERVAL)
    raise AssertionError(f"Condition never became true within {POLL_TIMEOUT}s: {sql}")


def worker_died_because(worker: subprocess.Popen[bytes]) -> str:
    """
    The worker's stderr, for when it never reached the state we were waiting for.

    Without this, a worker that fails at import time looks identical to one that is
    merely slow: the test times out with no clue why. Diagnosing that by hand once is
    enough.
    """
    if worker.poll() is None:
        return "(worker still running)"
    _, err = worker.communicate(timeout=5)
    return err.decode(errors="replace")[-2000:]


async def counts(engine: AsyncEngine, run_id: uuid.UUID) -> tuple[int, int, int]:
    """(executions of the tool body, surviving effects, committed step rows)."""
    async with engine.connect() as conn:
        attempts = (
            await conn.execute(
                text("SELECT count(*) FROM test_side_effect_attempts WHERE run_id = :r"),
                {"r": run_id},
            )
        ).scalar_one()
        effects = (
            await conn.execute(
                text("SELECT count(*) FROM test_side_effects WHERE run_id = :r"),
                {"r": run_id},
            )
        ).scalar_one()
        steps = (
            await conn.execute(
                text("SELECT count(*) FROM run_steps WHERE run_id = :r"), {"r": run_id}
            )
        ).scalar_one()
    return int(attempts), int(effects), int(steps)


def spawn_worker(run_id: uuid.UUID, mode: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "tests.integration._kill9_worker", str(run_id), mode],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


async def create_run(store: PostgresRunStore) -> uuid.UUID:
    record = await store.create(
        NewRun(
            agent="tester",
            input={"query": "apply the side effect"},
            limits=RunLimits(max_steps=6, token_budget=50_000),
            effort="medium",
            prompt_name="test",
            prompt_version=1,
            model="fake-model-1",
        )
    )
    return record.id


async def finish_the_run(sessions: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> None:
    """
    A replacement worker takes over and drives the run to completion.

    Note what it is given: the *same* script the dead worker had. A resumed run really
    does re-ask the model, because the step was never committed — so the replacement
    sees the same tool call again and it is the ledger, not the conversation, that
    prevents the second execution.
    """
    registry = ToolRegistry()
    registry.register(make_side_effect_tool(sessions, run_id))
    loop = AgentLoop(
        llm=FakeLLM(script=[calls_tool("side_effect", {"label": "once"}), says("done")]),
        registry=registry,
        store=PostgresRunStore(sessions),
        ledger=PostgresToolLedger(sessions),
        clock=SystemClock(),
        load_prompt=lambda _n, _v: "You are a test agent.",
        lease_ttl_seconds=30,
    )
    await loop.run(run_id, owner="replacement-worker")


# --- t2-t3: the window the ledger closes ---------------------------------------------


async def test_killed_between_the_ledger_and_the_commit_the_tool_never_runs_twice(
    store: PostgresRunStore,
    session_factory: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
) -> None:
    """
    The strongest form of the claim: one attempt, one outcome, across a real kill -9.

    The worker runs the tool, records SUCCEEDED in the ledger, and is destroyed while
    committing the step. The replacement re-asks the model, gets the same tool call
    back, and the ledger hands over the recorded result instead of executing. The tool
    body never runs a second time.
    """
    run_id = await create_run(store)
    worker = spawn_worker(run_id, "slow_commit")
    try:
        await wait_until(
            engine,
            "SELECT count(*) > 0 FROM tool_invocations WHERE run_id = :r AND status = 'SUCCEEDED'",
            {"r": run_id},
        )
        worker.kill()
    except AssertionError as exc:  # pragma: no cover - only on a broken worker
        raise AssertionError(f"{exc}\nworker stderr:\n{worker_died_because(worker)}") from exc
    finally:
        worker.wait(timeout=10)

    attempts, effects, steps = await counts(engine, run_id)
    assert (attempts, effects, steps) == (1, 1, 0)  # ran once, nothing committed

    await asyncio.sleep(3.5)  # the dead worker's 3s lease has to lapse
    await finish_the_run(session_factory, run_id)

    attempts, effects, steps = await counts(engine, run_id)
    assert attempts == 1, "the tool body executed more than once"
    assert effects == 1
    assert steps == 2, "expected exactly one row per completed step, with no duplicates"

    record = await store.load(run_id)
    assert record.status is RunStatus.COMPLETED
    assert record.answer == "done"


# --- t1-t2: the window that cannot be closed ------------------------------------------


async def test_killed_mid_tool_the_effect_still_happens_exactly_once(
    store: PostgresRunStore,
    session_factory: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
) -> None:
    """
    The honest version of the claim, and the one worth being precise about.

    Here the worker dies *during* the tool, after the write but before the ledger is
    told. On resume the ledger row says PENDING, which means genuinely nobody knows
    whether the effect landed — so an IDEMPOTENT_WRITE tool is executed again.

    **Two attempts. One outcome.** The second attempt collapses into the first because
    the tool's downstream deduplicates on a natural key, which is exactly what declaring
    IDEMPOTENT_WRITE promises. This is not exactly-once execution and I would not claim
    it is; it is at-least-once execution with effectively-once outcomes.
    """
    run_id = await create_run(store)
    worker = spawn_worker(run_id, "slow_tool")
    try:
        await wait_until(
            engine,
            "SELECT count(*) > 0 FROM test_side_effect_attempts WHERE run_id = :r",
            {"r": run_id},
        )
        worker.kill()
    except AssertionError as exc:  # pragma: no cover - only on a broken worker
        raise AssertionError(f"{exc}\nworker stderr:\n{worker_died_because(worker)}") from exc
    finally:
        worker.wait(timeout=10)

    attempts, effects, steps = await counts(engine, run_id)
    assert (attempts, effects, steps) == (1, 1, 0)

    async with engine.connect() as conn:
        status = (
            await conn.execute(
                text("SELECT status FROM tool_invocations WHERE run_id = :r"), {"r": run_id}
            )
        ).scalar_one()
    assert status == "PENDING", "the ledger must record the ambiguity, not resolve it"

    await asyncio.sleep(3.5)
    await finish_the_run(session_factory, run_id)

    attempts, effects, steps = await counts(engine, run_id)
    assert attempts == 2, "an ambiguous IDEMPOTENT_WRITE is expected to re-execute"
    assert effects == 1, "but the world must only have been changed once"
    assert steps == 2

    record = await store.load(run_id)
    assert record.status is RunStatus.COMPLETED


# --- What a second worker must not do -------------------------------------------------


async def test_a_replacement_cannot_take_over_while_the_lease_is_still_live(
    store: PostgresRunStore,
    session_factory: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
) -> None:
    """
    The lease is what stops recovery from becoming the problem it was meant to solve.

    A crashed worker's run must *not* be grabbed instantly by a replacement, because
    "crashed" and "slow" look identical from outside. The replacement waits for the
    lease to lapse; until then it is refused.
    """
    from app.core.runtime.errors import RunNotResumable

    run_id = await create_run(store)
    worker = spawn_worker(run_id, "slow_tool")
    try:
        await wait_until(
            engine,
            "SELECT count(*) > 0 FROM test_side_effect_attempts WHERE run_id = :r",
            {"r": run_id},
        )
        worker.kill()

        # The process is gone, but its lease has not expired yet.
        with pytest.raises(RunNotResumable):
            await finish_the_run(session_factory, run_id)
    except AssertionError as exc:  # pragma: no cover - only on a broken worker
        raise AssertionError(f"{exc}\nworker stderr:\n{worker_died_because(worker)}") from exc
    finally:
        worker.wait(timeout=10)

    await asyncio.sleep(3.5)
    await finish_the_run(session_factory, run_id)

    assert (await store.load(run_id)).status is RunStatus.COMPLETED
