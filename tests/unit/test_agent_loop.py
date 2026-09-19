"""
Unit tests for the ReAct loop. No network, no database, no model weights.

Half of these assert on the *request* rather than the result, because that is where the
subtle agent-loop bugs live: results split across two user messages, a missing
tool_use_id, a thinking block that did not survive the round trip. None of those raise
an exception. They degrade behaviour quietly, which is why they need assertions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from app.adapters.db.memory import InMemoryRunStore, InMemoryToolLedger
from app.adapters.llm.fake_llm import (
    FakeLLM,
    ScriptExhausted,
    calls_tool,
    calls_tools,
    refuses,
    responds,
    says,
    truncated,
)
from app.core.runtime.loop import MAX_TOOL_RESULT_CHARS, AgentLoop
from app.core.runtime.messages import (
    OpaqueBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from app.core.runtime.state import ErrorCode, NewRun, RunLimits, RunOutcome, RunStatus
from app.core.tools.base import EffectClass, ToolSpec
from app.core.tools.builtin.calculator import calculator_tool
from app.core.tools.builtin.clock import clock_tool
from app.core.tools.registry import ToolRegistry
from tests.unit.test_builtin_tools import FrozenClock

SYSTEM = "You are a test agent."
OWNER = "test-worker"


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


@dataclass
class Harness:
    """
    Creates a run and drives it, so tests read the way they did before durability.

    The loop under test is the real one — the same object that runs against Postgres.
    Only the store and ledger are in-memory, which is the whole point of the ports:
    there is no second loop for tests to accidentally verify instead.
    """

    loop: AgentLoop
    store: InMemoryRunStore
    ledger: InMemoryToolLedger
    clock: RecordingClock
    limits: RunLimits
    run_id: uuid.UUID | None = None

    async def run(self, user_input: str, *, owner: str = OWNER) -> RunOutcome:
        if self.run_id is None:
            record = await self.store.create(
                NewRun(
                    agent="tester",
                    input={"query": user_input},
                    limits=self.limits,
                    effort="medium",
                    prompt_name="test",
                    prompt_version=1,
                    model="fake-model-1",
                )
            )
            self.run_id = record.id
        return await self.loop.run(self.run_id, owner=owner)


def build_loop(
    script: list[object],
    *,
    limits: RunLimits | None = None,
    registry: ToolRegistry | None = None,
    store: InMemoryRunStore | None = None,
    ledger: InMemoryToolLedger | None = None,
) -> tuple[Harness, FakeLLM]:
    llm = FakeLLM(script=script)  # type: ignore[arg-type]
    if registry is None:
        registry = ToolRegistry()
        registry.register(calculator_tool())
        registry.register(clock_tool(FrozenClock(datetime(2026, 9, 19, tzinfo=UTC))))
    store = store or InMemoryRunStore()
    ledger = ledger or InMemoryToolLedger()
    clock = RecordingClock()
    loop = AgentLoop(
        llm=llm,
        registry=registry,
        store=store,
        ledger=ledger,
        clock=clock,
        load_prompt=lambda _name, _version: SYSTEM,
        # Fixed jitter draw: the delay schedule is then exactly predictable.
        rng=lambda: 1.0,
    )
    return (
        Harness(loop=loop, store=store, ledger=ledger, clock=clock, limits=limits or RunLimits()),
        llm,
    )


# --- The happy path ------------------------------------------------------------------


async def test_a_tool_call_then_an_answer() -> None:
    """The v0 exit criterion: call a tool, feed the result back, stop on end_turn."""
    loop, llm = build_loop(
        [calls_tool("calculator", {"expression": "2 + 2"}), says("The answer is 4.")]
    )

    outcome = await loop.run("What is 2 + 2?")

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.answer == "The answer is 4."
    assert outcome.usage.steps == 2
    assert [s.stop_reason for s in outcome.steps] == ["tool_use", "end_turn"]
    assert outcome.steps[0].tool_calls[0].name == "calculator"
    assert outcome.steps[0].tool_calls[0].ok is True


async def test_the_tool_result_is_fed_back_with_its_tool_use_id() -> None:
    """A result whose id does not match its call leaves the conversation malformed."""
    loop, llm = build_loop(
        [
            calls_tool("calculator", {"expression": "2 + 2"}, tool_use_id="toolu_abc"),
            says("4"),
        ]
    )

    await loop.run("What is 2 + 2?")

    second_request = llm.calls[1].messages
    result_message = second_request[-1]
    block = result_message.content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.tool_use_id == "toolu_abc"
    assert block.content == "4"
    assert block.is_error is False


async def test_the_answer_joins_every_text_block() -> None:
    """A turn can hold several text blocks; with citations on, it routinely does."""
    loop, _ = build_loop(
        [
            responds(
                stop_reason="end_turn",
                content=[TextBlock(text="The answer "), TextBlock(text="is 4.")],
            )
        ]
    )

    outcome = await loop.run("What is 2 + 2?")

    assert outcome.answer == "The answer is 4."


# --- The block protocol --------------------------------------------------------------


async def test_parallel_tool_calls_return_in_one_user_message() -> None:
    """
    CLAUDE.md §8: splitting results across several user messages silently teaches the
    model to stop making parallel calls. Nothing errors, so only an assertion catches it.
    """
    loop, llm = build_loop(
        [
            calls_tools(
                ("calculator", {"expression": "2 + 2"}, "toolu_1"),
                ("now", {}, "toolu_2"),
            ),
            says("Done."),
        ]
    )

    await loop.run("Calculate and tell me the time.")

    sent = llm.calls[1].messages
    # user, assistant, user — exactly three, not four.
    assert len(sent) == 3
    results = sent[-1].content
    assert len(results) == 2
    assert [b.tool_use_id for b in results if isinstance(b, ToolResultBlock)] == [
        "toolu_1",
        "toolu_2",
    ]


async def test_a_thinking_block_round_trips_verbatim() -> None:
    """
    The assistant turn goes back exactly as it arrived (D-014).

    A mangled thinking block invalidates the turn *and* the prompt cache, and neither
    failure raises anything.
    """
    loop, llm = build_loop(
        [
            calls_tool("calculator", {"expression": "2 + 2"}, thinking="I should compute this."),
            says("4"),
        ]
    )

    await loop.run("What is 2 + 2?")

    assistant_turn = llm.calls[1].messages[1]
    echoed = assistant_turn.content[0]
    assert isinstance(echoed, OpaqueBlock)
    assert echoed.model_dump() == {
        "type": "thinking",
        "thinking": "I should compute this.",
        "signature": "sig-fake",
    }


async def test_a_failed_tool_still_gets_a_result_block() -> None:
    """Dropping it leaves the model with a question it never got an answer to."""
    loop, llm = build_loop(
        [calls_tool("calculator", {"expression": "1 / 0"}), says("I could not compute that.")]
    )

    outcome = await loop.run("What is 1/0?")

    block = llm.calls[1].messages[-1].content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.is_error is True
    assert "Division by zero" in block.content
    assert outcome.status is RunStatus.COMPLETED
    assert outcome.steps[0].tool_calls[0].ok is False


async def test_an_unknown_tool_is_reported_to_the_model_not_crashed_on() -> None:
    """The model naming a tool that does not exist is a real case, not an impossible one."""
    loop, llm = build_loop([calls_tool("send_email", {"to": "x"}), says("I cannot do that.")])

    outcome = await loop.run("Email someone.")

    block = llm.calls[1].messages[-1].content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.is_error is True
    assert "send_email" in block.content
    assert "calculator" in block.content  # tells the model what it could have used
    assert outcome.status is RunStatus.COMPLETED


async def test_invalid_arguments_come_back_as_something_the_model_can_fix() -> None:
    loop, llm = build_loop(
        [
            calls_tool("calculator", {"wrong_field": "2 + 2"}),
            calls_tool("calculator", {"expression": "2 + 2"}, tool_use_id="toolu_2"),
            says("4"),
        ]
    )

    outcome = await loop.run("What is 2 + 2?")

    first_result = llm.calls[1].messages[-1].content[0]
    assert isinstance(first_result, ToolResultBlock)
    assert first_result.is_error is True
    assert "expression" in first_result.content
    assert outcome.answer == "4"


# --- stop_reason is read before content ----------------------------------------------


async def test_a_refusal_ends_the_run_without_reading_content() -> None:
    """
    A refusal is an HTTP 200 with an empty content list.

    A loop that reads `content` first dies here with a confusing error from an empty
    sequence, somewhere far from the actual cause.
    """
    loop, _ = build_loop([refuses(category="cyber")])

    outcome = await loop.run("Do something disallowed.")

    assert outcome.status is RunStatus.FAILED
    assert outcome.error_code is ErrorCode.UPSTREAM_REFUSAL
    assert "cyber" in (outcome.error_message or "")


async def test_a_truncated_turn_is_terminal_even_though_it_looks_valid() -> None:
    """
    The dangerous case: cut off mid tool_use.

    The response parses, the tool_use block is well-formed, and its `input` is empty
    because the model never finished writing it. Executing that is acting on half an
    instruction.
    """
    loop, _ = build_loop([truncated(partial_tool_call=True)])

    outcome = await loop.run("Compute something long.")

    assert outcome.status is RunStatus.FAILED
    assert outcome.error_code is ErrorCode.UPSTREAM_TRUNCATED


async def test_a_tool_use_turn_with_no_tool_use_block_is_refused() -> None:
    """Appending an empty user message would produce a malformed next request."""
    loop, _ = build_loop(
        [responds(stop_reason="tool_use", content=[TextBlock(text="thinking about it")])]
    )

    outcome = await loop.run("Compute something.")

    assert outcome.status is RunStatus.FAILED
    assert outcome.error_code is ErrorCode.UPSTREAM_TRUNCATED


# --- Budgets -------------------------------------------------------------------------


async def test_the_step_cap_stops_a_runaway_loop() -> None:
    """
    The most expensive bug this project can have, and the test that makes it impossible.

    The script would let the model call a tool forever; the cap is what stops it, and
    the run fails loudly rather than quietly continuing.
    """
    script = [calls_tool("calculator", {"expression": "1 + 1"}) for _ in range(10)]
    loop, llm = build_loop(script, limits=RunLimits(max_steps=3))

    outcome = await loop.run("Loop forever.")

    assert outcome.status is RunStatus.FAILED
    assert outcome.error_code is ErrorCode.BUDGET_EXCEEDED
    assert "step cap" in (outcome.error_message or "")
    assert len(llm.calls) == 3  # it stopped *before* the fourth request, not after


async def test_the_token_budget_stops_the_run_before_spending_more() -> None:
    """
    Checked before the request, never after.

    Checking afterwards would mean the budget is discovered to be blown by the very
    call that blew it, which is the one thing a spend limit exists to prevent.
    """
    script = [
        calls_tool("calculator", {"expression": "1 + 1"}, input_tokens=3_000) for _ in range(5)
    ]
    loop, llm = build_loop(script, limits=RunLimits(max_steps=20, token_budget=8_000))

    outcome = await loop.run("Spend.")

    assert outcome.error_code is ErrorCode.BUDGET_EXCEEDED
    assert "token budget" in (outcome.error_message or "")
    assert outcome.usage.total_tokens <= 8_000 + 3_020  # bounded overshoot, not unbounded


async def test_max_tokens_is_clamped_to_the_remaining_budget() -> None:
    """
    Bounds the overshoot: a step's cost cannot be known in advance, so the output
    ceiling shrinks as the budget is consumed.
    """
    loop, llm = build_loop(
        [calls_tool("calculator", {"expression": "1+1"}, input_tokens=110_000), says("done")],
        limits=RunLimits(token_budget=120_000, max_output_tokens=16_000),
    )

    await loop.run("Spend a lot.")

    # First call: the full ceiling, nothing spent yet.
    assert llm.calls[0].max_tokens == 16_000
    # Second: 110_000 in + 20 out are gone, so only 9_980 of budget remains and the
    # ceiling drops to match rather than allowing a 16_000-token overshoot.
    assert llm.calls[1].max_tokens == 120_000 - 110_020


# --- Tool bugs versus tool failures --------------------------------------------------


async def test_an_unexpected_tool_exception_fails_the_run_rather_than_the_worker() -> None:
    """
    A tool raising something other than ToolExecutionFailed is a bug in that tool.

    The run is recorded as failed. It is not fed back to the model as if it were an
    ordinary result — that would ship a bug to production disguised as something the
    model politely worked around — and it does not kill the process, which would take
    every other run sharing it.
    """
    from pydantic import BaseModel

    class Empty(BaseModel):
        pass

    async def explodes(payload: Empty) -> str:
        raise RuntimeError("I am a bug, not a failure mode")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="broken",
            description="A tool with a bug in it.",
            input_model=Empty,
            handler=explodes,
            effect_class=EffectClass.READ_ONLY,
        )
    )
    loop, _ = build_loop([calls_tool("broken", {})], registry=registry)

    outcome = await loop.run("Use the broken tool.")

    assert outcome.status is RunStatus.FAILED
    assert outcome.error_code is ErrorCode.TOOL_FAILED
    assert "RuntimeError" in (outcome.error_message or "")


async def test_an_oversized_tool_result_is_truncated() -> None:
    """
    The budget is checked before a step; a huge tool result arrives during one.

    Truncation is the only bound that does not require trusting every tool author.
    """
    from pydantic import BaseModel

    class Empty(BaseModel):
        pass

    async def floods(payload: Empty) -> str:
        return "x" * (MAX_TOOL_RESULT_CHARS * 3)

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="flood",
            description="Returns far too much.",
            input_model=Empty,
            handler=floods,
            effect_class=EffectClass.READ_ONLY,
        )
    )
    loop, llm = build_loop([calls_tool("flood", {}), says("ok")], registry=registry)

    await loop.run("Flood me.")

    block = llm.calls[1].messages[-1].content[0]
    assert isinstance(block, ToolResultBlock)
    assert len(block.content) < MAX_TOOL_RESULT_CHARS + 100
    assert "truncated" in block.content


# --- What the request looks like -----------------------------------------------------


async def test_the_tool_schemas_are_sent_sorted_on_every_call() -> None:
    """Cache-prefix stability, asserted at the point it actually matters (D-016)."""
    loop, llm = build_loop([calls_tool("calculator", {"expression": "1+1"}), says("2")])

    await loop.run("Add.")

    for call in llm.calls:
        assert [s.name for s in call.tools] == ["calculator", "now"]
        assert call.system == SYSTEM


async def test_the_script_running_out_is_a_loud_failure() -> None:
    """
    A loop that makes more calls than expected is a loop that is not terminating.

    Returning a default response instead would let a non-terminating loop pass its tests.
    """
    loop, _ = build_loop([calls_tool("calculator", {"expression": "1+1"})])

    with pytest.raises(ScriptExhausted):
        await loop.run("Add.")


async def test_a_transport_error_is_retried_then_pauses_the_run() -> None:
    """
    A provider outage is not a permanent failure, so it does not kill the run.

    The step is retried up to its bound, and when those run out the run is left PAUSED
    and resumable rather than FAILED. Marking it terminal would mean a five-minute
    upstream blip destroying every run in flight (D-009).
    """
    from app.core.runtime.errors import LLMTransportError

    loop, llm = build_loop(
        [LLMTransportError("connection reset")] * 3,
        limits=RunLimits(max_step_attempts=3),
    )

    outcome = await loop.run("Add.")

    assert outcome.status is RunStatus.PAUSED
    assert outcome.error_code is ErrorCode.STEP_FAILED
    assert len(llm.calls) == 3  # the bound was respected exactly


async def test_backoff_grows_and_is_jittered() -> None:
    """
    Full jitter, asserted exactly because the random draw is injected.

    A fixed exponential would have a hundred rate-limited runs retrying in lockstep and
    re-triggering the limit together — the delay would move the stampede, not break it.
    With `rng` pinned to 1.0 the schedule is the upper edge of each window.
    """
    from app.core.runtime.errors import LLMTransportError

    loop, _ = build_loop([LLMTransportError("boom")] * 4, limits=RunLimits(max_step_attempts=4))

    await loop.run("Add.")

    assert loop.clock.slept == [0.5, 1.0, 2.0]


async def test_the_full_conversation_is_returned_for_replay() -> None:
    """
    v1 resumes by replaying this. Reconstructing it from the step records instead would
    be a second chance to get the round trip wrong.
    """
    loop, _ = build_loop(
        [calls_tool("calculator", {"expression": "2 + 2"}, thinking="hmm"), says("4")]
    )

    outcome = await loop.run("What is 2 + 2?")

    roles = [m.role for m in outcome.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert isinstance(outcome.messages[1].content[1], ToolUseBlock)
    assert isinstance(outcome.messages[2].content[0], ToolResultBlock)
