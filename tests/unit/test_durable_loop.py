"""
Crash and resume, at the loop level. No database — the store and ledger are in-memory.

These are the tests for the behaviour the resume bullet claims. They run here, with
fakes, because the *control flow* is what is being tested: what the loop does when it
comes back to a run that is half finished. Whether Postgres really gives us atomicity
and `SKIP LOCKED` is a different question, tested against a real database in
`tests/integration/`.

The instrument in most of these is a tool that counts how many times it actually ran.
"Executed exactly once" is not a property you can assert by inspecting state afterwards;
you have to count.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from app.adapters.db.memory import InMemoryRunStore, InMemoryToolLedger
from app.adapters.llm.fake_llm import FakeLLM, calls_tool, says
from app.core.runtime.errors import (
    LLMTransportError,
    RunNotResumable,
    StepAlreadyCommitted,
)
from app.core.runtime.loop import AgentLoop
from app.core.runtime.messages import Message
from app.core.runtime.state import (
    ErrorCode,
    NewRun,
    RunLimits,
    RunOutcome,
    RunStatus,
    StepRecord,
)
from app.core.tools.base import EffectClass, ToolSpec
from app.core.tools.registry import ToolRegistry

SYSTEM = "You are a test agent."
OWNER = "worker-1"


class RecordingClock:
    """
    A `Clock` that never actually sleeps and remembers what it was asked to wait.

    This is what the Clock port was defined for in v0.2 and the first place it pays: the
    retry tests assert the exact backoff schedule in microseconds instead of taking
    several real seconds and being flaky about it.
    """

    def __init__(self) -> None:
        self.slept: list[float] = []

    def now(self) -> datetime:
        return datetime(2026, 9, 19, tzinfo=UTC)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class Empty(BaseModel):
    pass


@dataclass
class CountingTool:
    """
    A tool that records every genuine execution.

    The counter is the whole instrument: "the tool ran exactly once across a crash" is
    only demonstrable by counting executions, never by looking at the result.
    """

    effect_class: EffectClass = EffectClass.IDEMPOTENT_WRITE
    executions: list[str] = field(default_factory=list)

    def spec(self) -> ToolSpec[Empty]:
        async def handler(payload: Empty) -> str:
            self.executions.append("ran")
            return f"execution #{len(self.executions)}"

        return ToolSpec(
            name="side_effect",
            description="A tool with a side effect we can count.",
            input_model=Empty,
            handler=handler,
            effect_class=self.effect_class,
        )


class CrashingStore:
    """
    Wraps a store and fails one `commit_step`, simulating a process dying at t3.

    The interesting crash is not "the tool failed" — it is the process disappearing in
    the gap between the tool finishing and the step being recorded. Nothing in the
    system raises at that moment; the evidence is simply missing afterwards.
    """

    def __init__(self, inner: InMemoryRunStore, fail_on_step: int) -> None:
        self._inner = inner
        self._fail_on_step = fail_on_step
        self.crashed = False

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def commit_step(
        self, run_id: uuid.UUID, step: StepRecord, messages: Sequence[Message]
    ) -> None:
        if step.idx == self._fail_on_step and not self.crashed:
            self.crashed = True
            raise RuntimeError("process died before the step was committed")
        await self._inner.commit_step(run_id, step, messages)


def build(
    script: list[object],
    tool: CountingTool,
    *,
    store: object | None = None,
    ledger: InMemoryToolLedger | None = None,
) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(tool.spec())
    return AgentLoop(
        llm=FakeLLM(script=script),  # type: ignore[arg-type]
        registry=registry,
        store=store or InMemoryRunStore(),  # type: ignore[arg-type]
        ledger=ledger or InMemoryToolLedger(),
        clock=RecordingClock(),
        load_prompt=lambda _n, _v: SYSTEM,
        rng=lambda: 1.0,
    )


async def create_run(store: InMemoryRunStore, **overrides: object) -> uuid.UUID:
    spec: dict[str, object] = {
        "agent": "tester",
        "input": {"query": "do the thing"},
        # One attempt per step, so a scripted transport failure stops the worker
        # immediately instead of consuming the script on retries.
        "limits": RunLimits(max_step_attempts=1),
        "effort": "medium",
        "prompt_name": "test",
        "prompt_version": 1,
        "model": "fake-model-1",
    }
    spec.update(overrides)
    record = await store.create(NewRun(**spec))  # type: ignore[arg-type]
    return record.id


# --- Resuming from a committed step --------------------------------------------------


async def test_a_resumed_run_continues_from_the_last_committed_step() -> None:
    """
    The resume bullet, at its simplest.

    The first worker commits step 0 and then dies during the *next* model call. The
    second worker loads the run, sees one committed step, and carries on — it does not
    start over, and it does not re-run the tool.
    """
    store = InMemoryRunStore()
    ledger = InMemoryToolLedger()
    tool = CountingTool()
    run_id = await create_run(store)

    first = build(
        [calls_tool("side_effect", {}), LLMTransportError("connection reset")],
        tool,
        store=store,
        ledger=ledger,
    )
    paused = await first.run(run_id, owner=OWNER)
    assert paused.status is RunStatus.PAUSED  # resumable, not dead

    store.expire_lease(run_id)  # the worker is gone; its lease times out
    second = build([says("all done")], tool, store=store, ledger=ledger)
    outcome = await second.run(run_id, owner="worker-2")

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.answer == "all done"
    assert tool.executions == ["ran"]  # exactly once, across two workers
    assert len(outcome.steps) == 2  # step 0 from worker-1, step 1 from worker-2


async def test_the_resumed_conversation_carries_the_committed_history() -> None:
    """
    The second worker sends the first worker's turns back to the model.

    Without this the model would answer with no memory of the tool result it asked for,
    which looks like the run working and produces a wrong answer.
    """
    store = InMemoryRunStore()
    ledger = InMemoryToolLedger()
    tool = CountingTool()
    run_id = await create_run(store)

    first = build(
        [calls_tool("side_effect", {}), LLMTransportError("boom")], tool, store=store, ledger=ledger
    )
    await first.run(run_id, owner=OWNER)

    store.expire_lease(run_id)
    llm = FakeLLM(script=[says("done")])
    registry = ToolRegistry()
    registry.register(tool.spec())
    second = AgentLoop(
        llm=llm,
        registry=registry,
        store=store,
        ledger=ledger,
        clock=RecordingClock(),
        load_prompt=lambda _n, _v: SYSTEM,
    )
    await second.run(run_id, owner="worker-2")

    replayed = llm.calls[0].messages
    assert [m.role for m in replayed] == ["user", "assistant", "user"]


# --- The t2-t3 window: the tool ran, the step did not commit -------------------------


async def test_a_crash_after_the_tool_but_before_the_commit_does_not_re_execute() -> None:
    """
    The window D-021 closes, demonstrated end to end.

    The tool runs, the ledger records SUCCEEDED, and *then* the process dies before the
    step row lands. The resumed run redoes the step — it has to, the step was never
    committed — but the ledger recognises the call and hands back the recorded result.
    The side effect happens once.

    Without the ledger this is the bug that charges a customer twice, and nothing
    anywhere raises.
    """
    inner = InMemoryRunStore()
    crashing = CrashingStore(inner, fail_on_step=0)
    ledger = InMemoryToolLedger()
    tool = CountingTool()
    run_id = await create_run(inner)

    first = build([calls_tool("side_effect", {})], tool, store=crashing, ledger=ledger)
    with pytest.raises(RuntimeError, match="died before the step"):
        await first.run(run_id, owner=OWNER)

    assert tool.executions == ["ran"]  # it definitely ran
    inner.expire_lease(run_id)

    second = build(
        [calls_tool("side_effect", {}), says("finished")], tool, store=inner, ledger=ledger
    )
    outcome = await second.run(run_id, owner="worker-2")

    assert outcome.status is RunStatus.COMPLETED
    assert tool.executions == ["ran"]  # STILL once — the replay did not re-execute
    assert len(ledger.executions) == 1


async def test_the_replayed_result_is_the_one_that_was_recorded() -> None:
    """Not just "did not run again" — the model must see the original output."""
    inner = InMemoryRunStore()
    crashing = CrashingStore(inner, fail_on_step=0)
    ledger = InMemoryToolLedger()
    tool = CountingTool()
    run_id = await create_run(inner)

    with pytest.raises(RuntimeError):
        await build([calls_tool("side_effect", {})], tool, store=crashing, ledger=ledger).run(
            run_id, owner=OWNER
        )
    inner.expire_lease(run_id)

    llm = FakeLLM(script=[calls_tool("side_effect", {}), says("ok")])
    registry = ToolRegistry()
    registry.register(tool.spec())
    await AgentLoop(
        llm=llm,
        registry=registry,
        store=inner,
        ledger=ledger,
        clock=RecordingClock(),
        load_prompt=lambda _n, _v: SYSTEM,
    ).run(run_id, owner="worker-2")

    results_turn = llm.calls[1].messages[-1]
    assert results_turn.content[0].content == "execution #1"  # type: ignore[union-attr]


# --- The window that cannot be closed ------------------------------------------------


async def test_an_interrupted_unsafe_tool_stops_the_run_for_review() -> None:
    """
    D-004's hard stop, reached through the loop.

    The ledger row is PENDING — the tool may or may not have run, and nothing can tell.
    For an UNSAFE tool the only honest options are re-charging someone or discarding the
    run, so the runtime does neither and asks a human.
    """
    store = InMemoryRunStore()
    ledger = InMemoryToolLedger()
    tool = CountingTool(effect_class=EffectClass.UNSAFE)
    run_id = await create_run(store)

    # Leave a PENDING row behind, exactly as a crash between t1 and t2 would.
    from app.core.runtime.idempotency import invocation_key

    key = invocation_key(run_id, 0, "side_effect", {})
    await ledger.begin(run_id, 0, "side_effect", EffectClass.UNSAFE, key)

    loop = build([calls_tool("side_effect", {})], tool, store=store, ledger=ledger)
    outcome = await loop.run(run_id, owner=OWNER)

    assert outcome.status is RunStatus.FAILED
    assert outcome.error_code is ErrorCode.NEEDS_REVIEW
    assert tool.executions == []  # it was not retried


async def test_an_interrupted_read_only_tool_simply_runs_again() -> None:
    """The same situation, a different declaration, a different and cheaper answer."""
    store = InMemoryRunStore()
    ledger = InMemoryToolLedger()
    tool = CountingTool(effect_class=EffectClass.READ_ONLY)
    run_id = await create_run(store)

    from app.core.runtime.idempotency import invocation_key

    key = invocation_key(run_id, 0, "side_effect", {})
    await ledger.begin(run_id, 0, "side_effect", EffectClass.READ_ONLY, key)

    loop = build([calls_tool("side_effect", {}), says("done")], tool, store=store, ledger=ledger)
    outcome = await loop.run(run_id, owner=OWNER)

    assert outcome.status is RunStatus.COMPLETED
    assert tool.executions == ["ran"]


# --- Leases --------------------------------------------------------------------------


async def test_a_second_worker_cannot_start_a_run_someone_else_holds() -> None:
    store = InMemoryRunStore()
    tool = CountingTool()
    run_id = await create_run(store)
    await store.claim(run_id, "worker-1", 60)

    loop = build([says("hi")], tool, store=store)
    with pytest.raises(RunNotResumable):
        await loop.run(run_id, owner="worker-2")


async def test_a_worker_that_loses_its_lease_stops_at_the_next_step_boundary() -> None:
    """
    The renewal check earning its place.

    A worker that stalled long enough to lose its lease must not keep writing steps
    beside its replacement. It finds out at the step boundary — before issuing another
    model call, so the damage is bounded at zero.
    """
    store = InMemoryRunStore()
    tool = CountingTool()
    run_id = await create_run(store)

    class StealingStore(CrashingStore):
        async def commit_step(
            self, run_id: uuid.UUID, step: StepRecord, messages: Sequence[Message]
        ) -> None:
            await self._inner.commit_step(run_id, step, messages)
            self._inner.expire_lease(run_id)
            await self._inner.claim(run_id, "worker-2", 60)

    loop = build(
        [calls_tool("side_effect", {}), says("never reached")],
        tool,
        store=StealingStore(store, fail_on_step=-1),
    )

    with pytest.raises(RunNotResumable, match="Lost the lease"):
        await loop.run(run_id, owner="worker-1")


async def test_a_completed_run_cannot_be_run_again() -> None:
    store = InMemoryRunStore()
    tool = CountingTool()
    run_id = await create_run(store)
    await build([says("done")], tool, store=store).run(run_id, owner=OWNER)

    with pytest.raises(RunNotResumable):
        await build([says("again")], tool, store=store).run(run_id, owner=OWNER)


# --- The duplicate-step guard --------------------------------------------------------


async def test_a_step_already_committed_is_treated_as_done_not_as_an_error() -> None:
    """
    `StepAlreadyCommitted` is an answer, not a failure.

    It means a previous attempt got its commit in before dying, so the work is recorded
    and repeating it would create the duplicate the constraint exists to refuse. The
    loop swallows it deliberately; this test pins that behaviour so nobody "fixes" it
    into a crash later.
    """
    store = InMemoryRunStore()
    tool = CountingTool()
    run_id = await create_run(store)

    class AlreadyCommittedStore(CrashingStore):
        """Commits for real, then reports the duplicate the loop must shrug off."""

        def __init__(self, inner: InMemoryRunStore) -> None:
            super().__init__(inner, fail_on_step=-1)
            self.raised = False

        async def commit_step(
            self, run_id: uuid.UUID, step: StepRecord, messages: Sequence[Message]
        ) -> None:
            await self._inner.commit_step(run_id, step, messages)
            self.raised = True
            raise StepAlreadyCommitted(f"Step {step.idx} is already committed.")

    wrapper = AlreadyCommittedStore(store)
    loop = build([says("done")], tool, store=wrapper)
    outcome = await loop.run(run_id, owner=OWNER)

    assert wrapper.raised  # the loop really did see it
    assert outcome.status is RunStatus.COMPLETED


async def test_the_terminal_outcome_is_persisted_not_just_returned() -> None:
    """A run whose answer lives only in the caller's memory is not a durable run."""
    store = InMemoryRunStore()
    tool = CountingTool()
    run_id = await create_run(store)

    await build([says("42")], tool, store=store).run(run_id, owner=OWNER)

    reloaded = await store.load(run_id)
    assert reloaded.status is RunStatus.COMPLETED
    assert reloaded.answer == "42"
    assert reloaded.lease_owner is None


async def test_a_budget_failure_is_persisted_with_its_code() -> None:
    store = InMemoryRunStore()
    tool = CountingTool()
    run_id = await create_run(store, limits=RunLimits(max_steps=1))

    outcome: RunOutcome = await build(
        [calls_tool("side_effect", {}), says("x")], tool, store=store
    ).run(run_id, owner=OWNER)

    assert outcome.error_code is ErrorCode.BUDGET_EXCEEDED
    reloaded = await store.load(run_id)
    assert reloaded.status is RunStatus.FAILED
    assert reloaded.error_code is ErrorCode.BUDGET_EXCEEDED
