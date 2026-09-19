"""
Per-step timeouts and the two retry bounds (D-009).

The clock is fake, so a sixty-second timeout is asserted in microseconds and the backoff
schedule is checked exactly rather than approximately. This is the whole reason `Clock`
is a port: a test that waits for real time to pass is slow *and* flaky, which is the one
combination guaranteed to get it deleted.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from app.adapters.db.memory import InMemoryRunStore, InMemoryToolLedger
from app.adapters.llm.fake_llm import FakeLLM, calls_tool, says
from app.core.runtime.errors import LLMRequestError, LLMTransportError
from app.core.runtime.loop import AgentLoop
from app.core.runtime.messages import Effort, LLMResponse, Message, ToolSchema
from app.core.runtime.retry import BackoffPolicy
from app.core.runtime.state import ErrorCode, NewRun, RunLimits, RunStatus
from app.core.tools.base import EffectClass, ToolSpec
from app.core.tools.registry import ToolRegistry


class RecordingClock:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def now(self) -> datetime:
        return datetime(2026, 9, 19, tzinfo=UTC)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class HangingLLM:
    """An `LLMClient` that never answers. The thing a per-step timeout exists for."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        *,
        messages: object,
        system: str,
        tools: object,
        max_tokens: int,
        effort: Effort,
    ) -> LLMResponse:
        self.calls += 1
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class Empty(BaseModel):
    pass


async def build(
    llm: object,
    *,
    limits: RunLimits,
    store: InMemoryRunStore | None = None,
) -> tuple[AgentLoop, uuid.UUID, RecordingClock, InMemoryRunStore]:
    store = store or InMemoryRunStore()
    clock = RecordingClock()
    registry = ToolRegistry()

    async def noop(payload: Empty) -> str:
        return "ok"

    registry.register(
        ToolSpec(
            name="noop",
            description="Does nothing.",
            input_model=Empty,
            handler=noop,
            effect_class=EffectClass.READ_ONLY,
        )
    )
    record = await store.create(
        NewRun(
            agent="tester",
            input={"query": "go"},
            limits=limits,
            effort="medium",
            prompt_name="test",
            prompt_version=1,
            model="fake-model-1",
        )
    )
    loop = AgentLoop(
        llm=llm,  # type: ignore[arg-type]
        registry=registry,
        store=store,
        ledger=InMemoryToolLedger(),
        clock=clock,
        load_prompt=lambda _n, _v: "system",
        backoff=BackoffPolicy(base_seconds=0.5, max_seconds=8.0),
        rng=lambda: 1.0,
    )
    return loop, record.id, clock, store


# --- The timeout ---------------------------------------------------------------------


async def test_a_hanging_model_call_is_cut_off_and_retried() -> None:
    """
    Without this, one wedged step holds its lease until the TTL expires while the worker
    waits politely — and the run makes no progress for as long as the upstream stays
    stuck.
    """
    llm = HangingLLM()
    loop, run_id, clock, _ = await build(
        llm, limits=RunLimits(step_timeout_seconds=0.05, max_step_attempts=3)
    )

    outcome = await loop.run(run_id, owner="w1")

    assert outcome.status is RunStatus.PAUSED
    assert outcome.error_code is ErrorCode.STEP_FAILED
    assert "exceeded its budget" in (outcome.error_message or "")
    assert llm.calls == 3  # exactly the bound, not one more


async def test_the_timeout_covers_the_tools_as_well_as_the_model() -> None:
    """
    One budget for the whole step, not one each.

    Two separate timeouts would let a step take twice as long as configured while each
    half stayed inside its own limit, which makes the setting mean nothing.
    """

    async def slow(payload: Empty) -> str:
        await asyncio.sleep(3600)
        return "never"

    store = InMemoryRunStore()
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="slow",
            description="Takes forever.",
            input_model=Empty,
            handler=slow,
            effect_class=EffectClass.READ_ONLY,
        )
    )
    record = await store.create(
        NewRun(
            agent="tester",
            input={"query": "go"},
            limits=RunLimits(step_timeout_seconds=0.05, max_step_attempts=1),
            effort="medium",
            prompt_name="test",
            prompt_version=1,
            model="fake-model-1",
        )
    )
    loop = AgentLoop(
        llm=FakeLLM(script=[calls_tool("slow", {})]),
        registry=registry,
        store=store,
        ledger=InMemoryToolLedger(),
        clock=RecordingClock(),
        load_prompt=lambda _n, _v: "system",
    )

    outcome = await loop.run(record.id, owner="w1")

    assert outcome.error_code is ErrorCode.STEP_FAILED


# --- What is and is not retried ------------------------------------------------------


async def test_a_bad_request_is_not_retried() -> None:
    """
    A 400 never becomes a 200. Retrying it spends money slowly and hides the real bug.

    This is the distinction the whole exception taxonomy exists for — and the reason
    CLAUDE.md §4 bans catching one broad SDK class.
    """
    llm = FakeLLM(script=[LLMRequestError("unknown model")])
    loop, run_id, clock, _ = await build(llm, limits=RunLimits(max_step_attempts=5))

    with pytest.raises(LLMRequestError):
        await loop.run(run_id, owner="w1")

    assert clock.slept == []  # no backoff, because there was no retry


async def test_a_transport_failure_is_retried_with_growing_backoff() -> None:
    llm = FakeLLM(script=[LLMTransportError("reset")] * 4)
    loop, run_id, clock, _ = await build(llm, limits=RunLimits(max_step_attempts=4))

    await loop.run(run_id, owner="w1")

    assert clock.slept == [0.5, 1.0, 2.0]


async def test_backoff_is_capped() -> None:
    """Unbounded growth turns a transient blip into a run that sleeps for an hour."""
    policy = BackoffPolicy(base_seconds=0.5, max_seconds=8.0)

    assert [policy.delay_for(n, 1.0) for n in range(1, 8)] == [
        0.0,
        0.5,
        1.0,
        2.0,
        4.0,
        8.0,
        8.0,
    ]


def test_jitter_spreads_retries_across_the_window() -> None:
    """
    Full jitter: the delay is uniform over [0, ceiling), not a fixed exponential.

    A hundred runs rate-limited at the same moment and backing off exactly 0.5s retry in
    lockstep and re-trigger the limit together. The delay moves the stampede; it does
    not break it.
    """
    policy = BackoffPolicy(base_seconds=1.0)

    assert policy.delay_for(3, 0.0) == 0.0
    assert policy.delay_for(3, 0.5) == 1.0
    assert policy.delay_for(3, 0.99) == pytest.approx(1.98)


# --- The durable bound ---------------------------------------------------------------


async def test_the_run_level_budget_survives_a_resume() -> None:
    """
    The reason the counter is a row and not a local variable.

    A worker that burns its step retries and stops has already spent part of the run's
    allowance. If a resumed worker got a fresh one, a crash loop would retry forever —
    the per-step bound alone cannot see across processes.
    """
    store = InMemoryRunStore()
    # One retry for the whole run. The first worker spends it; the second must not
    # be handed a fresh one.
    limits = RunLimits(max_step_attempts=2, max_run_retries=1)

    llm = FakeLLM(script=[LLMTransportError("a")] * 2)
    loop, run_id, _, _ = await build(llm, limits=limits, store=store)
    first = await loop.run(run_id, owner="w1")
    assert first.status is RunStatus.PAUSED

    assert (await store.load(run_id)).retries_used == 1

    store.expire_lease(run_id)
    llm2 = FakeLLM(script=[LLMTransportError("b")] * 2)
    registry = ToolRegistry()
    second = AgentLoop(
        llm=llm2,
        registry=registry,
        store=store,
        ledger=InMemoryToolLedger(),
        clock=RecordingClock(),
        load_prompt=lambda _n, _v: "system",
        rng=lambda: 1.0,
    )
    outcome = await second.run(run_id, owner="w2")

    # The run's own allowance ran out, so this is terminal rather than paused again.
    assert outcome.status is RunStatus.FAILED
    assert outcome.error_code is ErrorCode.STEP_FAILED
    assert "exhausted its 1 retries" in (outcome.error_message or "")


async def test_the_attempt_count_is_recorded_on_the_committed_step() -> None:
    """
    D-009: retries live in a row, not a loop counter.

    A counter vanishes on crash and is invisible in production. A column can be queried
    ("which tools retry most?") and alerted on.
    """
    store = InMemoryRunStore()
    llm = FakeLLM(script=[LLMTransportError("a"), says("done")])
    loop, run_id, _, _ = await build(llm, limits=RunLimits(max_step_attempts=3), store=store)

    outcome = await loop.run(run_id, owner="w1")

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.steps[0].attempt == 2
    assert (await store.load(run_id)).steps[0].attempt == 2


async def test_a_successful_first_try_records_attempt_one() -> None:
    llm = FakeLLM(script=[says("done")])
    loop, run_id, clock, _ = await build(llm, limits=RunLimits())

    outcome = await loop.run(run_id, owner="w1")

    assert outcome.steps[0].attempt == 1
    assert clock.slept == []


async def test_the_same_tool_at_two_steps_is_two_invocations() -> None:
    """
    The idempotency key includes `step_idx`, and this pins why.

    A model that calls the same tool with the same arguments at step 0 and again at
    step 3 means it genuinely wants the work done twice — the second call is a new
    decision, not a replay. Keying only on `(tool_name, args)` would silently collapse
    them and hand back a stale result, which would look like the deduplication working.

    (Note what this test does *not* claim: a retry *within* one step re-issues the model
    call before any tool has run, so the ledger is not what protects that path. The
    ledger protects across crashes and resumes, which `test_durable_loop.py` covers.)
    """
    executions: list[str] = []

    async def counted(payload: Empty) -> str:
        executions.append("ran")
        return f"value {len(executions)}"

    store = InMemoryRunStore()
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="counted",
            description="Counts executions.",
            input_model=Empty,
            handler=counted,
            effect_class=EffectClass.IDEMPOTENT_WRITE,
        )
    )
    record = await store.create(
        NewRun(
            agent="tester",
            input={"query": "go"},
            limits=RunLimits(),
            effort="medium",
            prompt_name="test",
            prompt_version=1,
            model="fake-model-1",
        )
    )
    loop = AgentLoop(
        llm=FakeLLM(
            script=[
                calls_tool("counted", {}, tool_use_id="t1"),
                calls_tool("counted", {}, tool_use_id="t2"),
                says("done"),
            ]
        ),
        registry=registry,
        store=store,
        ledger=InMemoryToolLedger(),
        clock=RecordingClock(),
        load_prompt=lambda _n, _v: "system",
    )

    outcome = await loop.run(record.id, owner="w1")

    assert outcome.status is RunStatus.COMPLETED
    assert executions == ["ran", "ran"], "different steps are different invocations"


__all__ = ["Message", "ToolSchema"]
