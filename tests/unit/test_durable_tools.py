"""
DURABLE tools: dispatch, waiting, and what survives the process dying (D-023).

The instrument, as in `test_durable_loop.py`, is a tool that counts its own executions.
"Exactly once across a crash" cannot be asserted by inspecting the result — a replayed
result and a re-executed one look identical — so it has to be counted.

No broker and no worker process. `FakeTaskQueue` stands in for both, and it executes the
tool through `execute_invocation`, which is the same function the real Celery task calls.
What is faked is the transport; the exactly-once logic under test is production code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from app.adapters.clock import SystemClock
from app.adapters.db.memory import InMemoryRunStore, InMemoryToolLedger
from app.adapters.llm.fake_llm import FakeLLM, calls_tool, says
from app.adapters.queue.fake_task_queue import Delivery, FakeTaskQueue
from app.core.runtime.errors import QueueNotConfigured
from app.core.runtime.idempotency import InvocationStatus, invocation_key
from app.core.runtime.loop import AgentLoop
from app.core.runtime.state import ErrorCode, NewRun, RunLimits, RunStatus
from app.core.tools.base import EffectClass, ExecutionMode, ToolSpec
from app.core.tools.execution import execute_invocation
from app.core.tools.registry import ToolRegistry

SYSTEM = "You are a test agent."
OWNER = "worker-1"
TOOL = "slow_write"

# Short, because two of these tests deliberately run a durable tool out of patience and
# the wait is real time. Half a second of budget leaves 0.4s of waiting after the loop's
# in-flight reserve — three poll intervals, enough to exercise the backoff and short
# enough that nobody deletes the test for being slow.
IMPATIENT = RunLimits(step_timeout_seconds=0.5, max_step_attempts=1)


class RecordingClock:
    """A `Clock` that never sleeps and remembers what it was asked to wait."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def now(self) -> datetime:
        return datetime(2026, 9, 22, tzinfo=UTC)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class Empty(BaseModel):
    pass


@dataclass
class CountingTool:
    """A DURABLE tool that records every genuine execution."""

    effect_class: EffectClass = EffectClass.IDEMPOTENT_WRITE
    executions: list[str] = field(default_factory=list)
    explode: bool = False

    def spec(self) -> ToolSpec[Empty]:
        async def handler(payload: Empty) -> str:
            self.executions.append("ran")
            if self.explode:
                raise RuntimeError("the tool is broken")
            return f"execution #{len(self.executions)}"

        return ToolSpec(
            name=TOOL,
            description="A slow tool with a side effect we can count.",
            input_model=Empty,
            handler=handler,
            effect_class=self.effect_class,
            execution=ExecutionMode.DURABLE,
        )


def build(
    script: list[object],
    tool: CountingTool,
    store: InMemoryRunStore,
    ledger: InMemoryToolLedger,
    *,
    mode: Delivery = Delivery.IMMEDIATE,
    queue: FakeTaskQueue | None = None,
    real_clock: bool = False,
) -> tuple[AgentLoop, FakeTaskQueue]:
    registry = ToolRegistry()
    registry.register(tool.spec())
    queue = queue or FakeTaskQueue(registry=registry, ledger=ledger, mode=mode)
    loop = AgentLoop(
        llm=FakeLLM(script=script),  # type: ignore[arg-type]
        registry=registry,
        store=store,
        ledger=ledger,
        # A real clock where the test waits out a deadline, so the poll sleeps instead of
        # spinning; the recording one everywhere else, so nothing takes real time.
        clock=SystemClock() if real_clock else RecordingClock(),  # type: ignore[arg-type]
        load_prompt=lambda _n, _v: SYSTEM,
        queue=queue,
        rng=lambda: 1.0,
    )
    return loop, queue


async def create_run(store: InMemoryRunStore, *, limits: RunLimits | None = None) -> uuid.UUID:
    record = await store.create(
        NewRun(
            agent="tester",
            input={"query": "do the slow thing"},
            limits=limits or RunLimits(max_step_attempts=1),
            effort="medium",
            prompt_name="test",
            prompt_version=1,
            model="fake-model-1",
        )
    )
    return record.id


# --- Dispatch ------------------------------------------------------------------------


async def test_a_durable_tool_is_dispatched_and_its_result_comes_back_through_the_ledger() -> None:
    """
    The happy path, and the shape of the whole mode: the loop never touches the handler.

    What proves it is not the answer but `queue.dispatched` — the tool ran because a
    worker picked it up, and the loop learned about it by reading a row.
    """
    store = InMemoryRunStore()
    ledger = InMemoryToolLedger()
    tool = CountingTool()
    run_id = await create_run(store)

    loop, queue = build([calls_tool(TOOL, {}), says("done")], tool, store, ledger)
    outcome = await loop.run(run_id, owner=OWNER)

    assert outcome.status is RunStatus.COMPLETED
    assert len(queue.dispatched) == 1
    assert tool.executions == ["ran"]

    # The result reached the model as an ordinary tool_result, indistinguishable from an
    # inline one. That indistinguishability is the point of the mode.
    results = [b for m in outcome.messages for b in m.content if getattr(b, "content", None)]
    assert any("execution #1" in str(getattr(b, "content", "")) for b in results)


async def test_an_unreachable_broker_is_retried_rather_than_failing_the_run() -> None:
    """
    A publish that failed means the tool provably did not start, so retrying risks
    nothing. Failing the run instead would make Redis a single point of failure for work
    that had not even begun.
    """
    store = InMemoryRunStore()
    ledger = InMemoryToolLedger()
    tool = CountingTool()
    run_id = await create_run(store, limits=RunLimits(max_step_attempts=2))

    loop, queue = build(
        [calls_tool(TOOL, {}), calls_tool(TOOL, {})],
        tool,
        store,
        ledger,
        mode=Delivery.UNAVAILABLE,
    )
    outcome = await loop.run(run_id, owner=OWNER)

    assert outcome.status is RunStatus.PAUSED  # retries ran out; resumable, not dead
    assert outcome.error_code is ErrorCode.STEP_FAILED
    assert queue.dispatched == []
    assert tool.executions == []


async def test_a_durable_tool_without_a_queue_is_refused_when_the_loop_is_built() -> None:
    """
    A wiring mistake should stop the process, not the run. The registry is fixed at
    construction, so the answer is already knowable there — and the alternative is
    discovering it hours later, on the one step that happened to need the tool.
    """
    registry = ToolRegistry()
    registry.register(CountingTool().spec())

    with pytest.raises(QueueNotConfigured, match=TOOL):
        AgentLoop(
            llm=FakeLLM(script=[]),  # type: ignore[arg-type]
            registry=registry,
            store=InMemoryRunStore(),
            ledger=InMemoryToolLedger(),
            clock=RecordingClock(),  # type: ignore[arg-type]
            load_prompt=lambda _n, _v: SYSTEM,
        )


# --- Waiting, and running out of patience --------------------------------------------


async def test_a_tool_still_running_at_the_deadline_pauses_without_charging_a_retry() -> None:
    """
    Nothing has gone wrong, so nothing is failed and nothing is charged.

    The retry assertion is the one that matters. Burning the step's attempts here would
    spend an LLM call per attempt to rediscover a fact the ledger already knows, and
    would then mark the run STEP_FAILED for the crime of calling a slow tool.
    """
    store = InMemoryRunStore()
    ledger = InMemoryToolLedger()
    tool = CountingTool()
    run_id = await create_run(store, limits=IMPATIENT)

    loop, queue = build(
        [calls_tool(TOOL, {})], tool, store, ledger, mode=Delivery.DEFERRED, real_clock=True
    )
    outcome = await loop.run(run_id, owner=OWNER)

    assert outcome.status is RunStatus.PAUSED
    assert outcome.error_code is ErrorCode.TOOL_PENDING
    assert len(queue.dispatched) == 1  # dispatched once, then waited
    assert tool.executions == []  # the "worker" never picked it up

    record = await store.load(run_id)
    assert record.retries_used == 0


async def test_the_worker_finishes_while_the_run_is_dead_and_the_resume_collects_it() -> None:
    """
    This is what DURABLE actually buys, and the reason the mode exists at all.

    An inline tool killed mid-execution leaves the unanswerable t1-t2 window. A durable
    one does not run in the process that died: it finishes in its own worker, writes its
    row, and the resumed run finds the answer waiting.

    The load-bearing assertion is `dispatched == 1`. On resume the loop finds a ledger row
    and must **not** enqueue again — re-dispatching would duplicate the redelivery the
    broker already guarantees, which is how at-least-once quietly becomes at-least-twice.
    """
    store = InMemoryRunStore()
    ledger = InMemoryToolLedger()
    tool = CountingTool()
    run_id = await create_run(store, limits=IMPATIENT)

    # First worker: dispatches, waits, runs out of patience, pauses.
    first, queue = build(
        [calls_tool(TOOL, {})], tool, store, ledger, mode=Delivery.DEFERRED, real_clock=True
    )
    paused = await first.run(run_id, owner=OWNER)
    assert paused.status is RunStatus.PAUSED

    # The run is gone. The Celery worker, which never was that process, finishes anyway.
    await queue.deliver_all()
    assert tool.executions == ["ran"]

    # A second worker resumes. The model is asked again — one call, the price of not
    # holding a lease open indefinitely — and makes the same tool call, which hashes to
    # the same key and finds the finished row.
    store.expire_lease(run_id)
    second, _ = build(
        [calls_tool(TOOL, {}), says("done")],
        tool,
        store,
        ledger,
        queue=queue,
    )
    outcome = await second.run(run_id, owner="worker-2")

    assert outcome.status is RunStatus.COMPLETED
    assert tool.executions == ["ran"]
    assert len(queue.dispatched) == 1


# --- At-least-once delivery, made effectively-once ------------------------------------


async def test_a_redelivered_task_replays_its_result_instead_of_running_again() -> None:
    """
    `acks_late` plus `task_reject_on_worker_lost` means a task comes back. Nothing in
    Celery stops the side effect happening twice — the ledger row does, and this is the
    test that says so.
    """
    store = InMemoryRunStore()
    ledger = InMemoryToolLedger()
    tool = CountingTool()
    run_id = await create_run(store)

    loop, queue = build([calls_tool(TOOL, {}), says("done")], tool, store, ledger)
    await loop.run(run_id, owner=OWNER)
    assert tool.executions == ["ran"]

    await queue.redeliver()

    assert tool.executions == ["ran"]


async def test_a_redelivered_unsafe_task_stops_the_run_for_review() -> None:
    """
    The t1-t2 window, inside a worker this time.

    A task killed after its side effect but before recording it leaves PENDING. The
    redelivery finds that row, and for an UNSAFE tool the only honest answer is to stop —
    but the deciding process is a Celery worker with no channel back to the run except
    this row, which is why NEEDS_REVIEW is a *status* and not just an exception.
    """
    store = InMemoryRunStore()
    ledger = InMemoryToolLedger()
    tool = CountingTool(effect_class=EffectClass.UNSAFE)
    run_id = await create_run(store)
    registry = ToolRegistry()
    registry.register(tool.spec())

    # A worker that claimed the invocation and then died before completing it.
    key = invocation_key(run_id, 0, TOOL, {})
    await ledger.begin(run_id, 0, TOOL, EffectClass.UNSAFE, key)
    assert ledger.rows[key].status is InvocationStatus.PENDING

    # Celery hands the task to someone else. It must not execute.
    await execute_invocation(
        registry=registry,
        ledger=ledger,
        run_id=run_id,
        step_idx=0,
        tool_name=TOOL,
        args={},
        key=key,
    )
    assert tool.executions == []
    assert ledger.rows[key].status is InvocationStatus.NEEDS_REVIEW

    # The run, which knew none of that, reads the row and stops.
    queue = FakeTaskQueue(registry=registry, ledger=ledger, mode=Delivery.DEFERRED)
    loop, _ = build([calls_tool(TOOL, {})], tool, store, ledger, queue=queue)
    outcome = await loop.run(run_id, owner=OWNER)

    assert outcome.status is RunStatus.FAILED
    assert outcome.error_code is ErrorCode.NEEDS_REVIEW
    assert tool.executions == []


async def test_a_tool_that_crashes_in_a_worker_closes_its_row_instead_of_hanging() -> None:
    """
    A bug in a durable tool must not look like patience.

    The exception dies in the worker — it cannot cross a process boundary — so if the row
    were left PENDING the run would wait out its budget and pause, and every resume would
    do it again. Recording the failure before re-raising is what turns an invisible hang
    into a tool_result the model can react to.
    """
    store = InMemoryRunStore()
    ledger = InMemoryToolLedger()
    tool = CountingTool(explode=True)
    run_id = await create_run(store)

    loop, queue = build(
        [calls_tool(TOOL, {}), says("I'll try something else")], tool, store, ledger
    )
    outcome = await loop.run(run_id, owner=OWNER)

    assert outcome.status is RunStatus.COMPLETED
    assert len(queue.crashes) == 1  # the traceback stayed in the "worker"
    assert tool.executions == ["ran"]

    key = invocation_key(run_id, 0, TOOL, {})
    assert ledger.rows[key].status is InvocationStatus.FAILED
    assert ledger.rows[key].is_error


# --- The worker's half, tested directly ----------------------------------------------


async def test_execute_invocation_runs_the_tool_once_however_often_it_is_called() -> None:
    """
    The Celery task is a thin shell around this function, so this is the task's own
    exactly-once guarantee, tested with no broker and no process.
    """
    ledger = InMemoryToolLedger()
    tool = CountingTool()
    registry = ToolRegistry()
    registry.register(tool.spec())
    run_id = uuid.uuid4()
    key = invocation_key(run_id, 3, TOOL, {})

    outcomes = [
        await execute_invocation(
            registry=registry,
            ledger=ledger,
            run_id=run_id,
            step_idx=3,
            tool_name=TOOL,
            args={},
            key=key,
        )
        for _ in range(4)
    ]

    assert tool.executions == ["ran"]
    assert {o.status for o in outcomes} == {InvocationStatus.SUCCEEDED}
    assert {o.result for o in outcomes} == {"execution #1"}
