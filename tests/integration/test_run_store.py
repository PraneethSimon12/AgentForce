"""
Integration tests for the Postgres run store. Real database, real transactions.

The claim under test is the resume bullet's foundation: **a step is either fully
committed or it never happened, and a conversation replayed out of Postgres is
byte-identical to the one that was stored.** Neither can be demonstrated against a fake,
because both are properties Postgres provides.
"""

from __future__ import annotations

import uuid

import pytest

from app.adapters.db.run_store import PostgresRunStore
from app.core.runtime.errors import RunNotFound, StepAlreadyCommitted
from app.core.runtime.messages import (
    Message,
    OpaqueBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    blocks_to_wire,
)
from app.core.runtime.state import (
    ErrorCode,
    NewRun,
    RunLimits,
    RunOutcome,
    RunStatus,
    RunUsage,
    StepRecord,
    ToolCallRecord,
)

pytestmark = pytest.mark.integration


def new_run(**overrides: object) -> NewRun:
    spec: dict[str, object] = {
        "agent": "researcher",
        "input": {"query": "What is 2 + 2?"},
        "limits": RunLimits(max_steps=6, token_budget=50_000),
        "effort": "medium",
        "prompt_name": "agent",
        "prompt_version": 1,
        "model": "claude-opus-5",
    }
    spec.update(overrides)
    return NewRun(**spec)  # type: ignore[arg-type]


def a_step(idx: int, *, tokens: int = 1_000) -> StepRecord:
    return StepRecord(
        idx=idx,
        stop_reason="tool_use",
        usage=Usage(input_tokens=tokens, output_tokens=20, cache_read_input_tokens=5),
        tool_calls=(ToolCallRecord(tool_use_id=f"toolu_{idx}", name="calculator", ok=True),),
    )


# --- Creation ------------------------------------------------------------------------


async def test_a_run_is_recorded_before_it_starts(store: PostgresRunStore) -> None:
    """plan.md §2.2: `POST /v1/runs` records the run; it does not start it."""
    record = await store.create(new_run())

    assert record.status is RunStatus.QUEUED
    assert record.usage.steps == 0
    assert record.messages == ()
    assert record.next_step_idx == 0


async def test_the_limits_are_frozen_onto_the_run(store: PostgresRunStore) -> None:
    """
    A resumed run must use the budget it started with.

    Re-reading settings at resume time would let a config change silently move the cost
    ceiling of a run already in flight, with nothing in the audit trail to show it.
    """
    record = await store.create(new_run(limits=RunLimits(max_steps=3, token_budget=9_000)))

    reloaded = await store.load(record.id)

    assert reloaded.limits.max_steps == 3
    assert reloaded.limits.token_budget == 9_000


async def test_the_same_idempotency_key_returns_the_original_run(
    store: PostgresRunStore,
) -> None:
    """
    A client retrying a timed-out POST must not start a second run.

    Enforced by the unique constraint, not by a read-then-write — the latter has a
    window between the check and the insert, which is the same lost-update shape as a
    lease claimed with "check then act".
    """
    first = await store.create(new_run(idempotency_key="abc-123"))
    second = await store.create(new_run(idempotency_key="abc-123"))

    assert first.id == second.id


async def test_different_tenants_may_reuse_a_key(store: PostgresRunStore) -> None:
    """Keys are chosen by clients, so they are only unique within a tenant."""
    first = await store.create(new_run(idempotency_key="k", tenant_id="acme"))
    second = await store.create(new_run(idempotency_key="k", tenant_id="globex"))

    assert first.id != second.id


async def test_loading_an_unknown_run_raises(store: PostgresRunStore) -> None:
    with pytest.raises(RunNotFound):
        await store.load(uuid.uuid4())


# --- Committing steps ----------------------------------------------------------------


async def test_committing_a_step_writes_the_row_the_messages_and_the_counters(
    store: PostgresRunStore,
) -> None:
    """One transaction, three writes. A crash on the next line loses nothing."""
    run = await store.create(new_run())

    await store.commit_step(
        run.id,
        a_step(0, tokens=1_200),
        [
            Message(role="assistant", content=[ToolUseBlock(id="t1", name="calculator", input={})]),
            Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content="4")]),
        ],
    )

    reloaded = await store.load(run.id)
    assert reloaded.status is RunStatus.RUNNING  # QUEUED flips on the first step
    assert len(reloaded.steps) == 1
    assert len(reloaded.messages) == 2
    assert reloaded.usage.input_tokens == 1_200
    assert reloaded.usage.cache_read_tokens == 5
    assert reloaded.next_step_idx == 1


async def test_committing_the_same_step_twice_is_refused(store: PostgresRunStore) -> None:
    """
    `UNIQUE(run_id, idx)` is the durability invariant.

    After a crash this is not an error so much as an answer: "did I already commit this
    step?" A duplicate row would mean a double-counted token bill and a conversation
    with a repeated turn, silently.
    """
    run = await store.create(new_run())
    await store.commit_step(run.id, a_step(0), [Message(role="assistant", content=[])])

    with pytest.raises(StepAlreadyCommitted):
        await store.commit_step(run.id, a_step(0), [Message(role="assistant", content=[])])

    reloaded = await store.load(run.id)
    assert len(reloaded.steps) == 1


async def test_a_refused_duplicate_leaves_nothing_behind(store: PostgresRunStore) -> None:
    """
    All-or-nothing. The rejected step must not leave its messages or its token count.

    This is the test that would catch the messages being written in a different
    transaction from the step — a bug that only shows up after a crash, as a
    conversation with an extra turn nobody can account for.
    """
    run = await store.create(new_run())
    await store.commit_step(
        run.id, a_step(0, tokens=1_000), [Message(role="assistant", content=[])]
    )

    with pytest.raises(StepAlreadyCommitted):
        await store.commit_step(
            run.id,
            a_step(0, tokens=9_999),
            [Message(role="assistant", content=[TextBlock(text="ghost")])],
        )

    reloaded = await store.load(run.id)
    assert len(reloaded.messages) == 1
    assert reloaded.usage.input_tokens == 1_000  # not 10_999


async def test_steps_and_messages_come_back_in_order(store: PostgresRunStore) -> None:
    run = await store.create(new_run())
    for idx in range(3):
        await store.commit_step(
            run.id,
            a_step(idx),
            [Message(role="assistant", content=[TextBlock(text=f"step {idx}")])],
        )

    reloaded = await store.load(run.id)

    assert [s.idx for s in reloaded.steps] == [0, 1, 2]
    assert [b.text for m in reloaded.messages for b in m.content if isinstance(b, TextBlock)] == [
        "step 0",
        "step 1",
        "step 2",
    ]


# --- Replay: the byte-identical guarantee --------------------------------------------


async def test_a_conversation_replays_byte_identically(store: PostgresRunStore) -> None:
    """
    Task 1.3, and the reason blocks are stored raw rather than rendered.

    A thinking block has to come back to the model *unchanged* or the turn is invalid —
    and the same bytes are the prompt-cache prefix, so one mistake breaks two things
    with nothing raised. Storing the block as JSONB and rebuilding it through
    `block_from_dict` means there is no rendering step to get wrong.
    """
    run = await store.create(new_run())
    original = [
        Message(role="user", content=[TextBlock(text="What is 48271 * 3319?")]),
        Message(
            role="assistant",
            content=[
                OpaqueBlock.model_validate(
                    {
                        "type": "thinking",
                        "thinking": "I should use the calculator.",
                        "signature": "ErUBCkYIBRgCIkDxk9Lm0vQ2",
                    }
                ),
                TextBlock(text="Let me compute that."),
                ToolUseBlock(
                    id="toolu_01A", name="calculator", input={"expression": "48271 * 3319"}
                ),
            ],
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="toolu_01A", content="160211449")],
        ),
    ]
    await store.commit_step(run.id, a_step(0), original)

    reloaded = await store.load(run.id)

    assert [m.role for m in reloaded.messages] == ["user", "assistant", "user"]
    for stored, loaded in zip(original, reloaded.messages, strict=True):
        assert blocks_to_wire(loaded.content) == blocks_to_wire(stored.content)

    # Explicitly: the signature survived. It is the field whose loss is silent.
    thinking = reloaded.messages[1].content[0]
    assert isinstance(thinking, OpaqueBlock)
    assert thinking.model_dump()["signature"] == "ErUBCkYIBRgCIkDxk9Lm0vQ2"


async def test_a_block_type_we_have_never_seen_survives_a_round_trip(
    store: PostgresRunStore,
) -> None:
    """A future API version must not make an old run unreplayable (D-019)."""
    run = await store.create(new_run())
    exotic = {"type": "block_from_2028", "payload": {"nested": [1, 2, {"deep": True}]}}
    await store.commit_step(
        run.id,
        a_step(0),
        [Message(role="assistant", content=[OpaqueBlock.model_validate(exotic)])],
    )

    reloaded = await store.load(run.id)

    assert reloaded.messages[0].content[0].model_dump() == exotic


# --- Finishing -----------------------------------------------------------------------


async def test_finishing_writes_the_terminal_state_and_releases_the_lease(
    store: PostgresRunStore,
) -> None:
    run = await store.create(new_run())
    await store.commit_step(run.id, a_step(0), [Message(role="assistant", content=[])])

    await store.finish(
        run.id,
        RunOutcome(
            status=RunStatus.COMPLETED,
            usage=RunUsage(steps=1),
            messages=(),
            answer="160211449",
        ),
    )

    reloaded = await store.load(run.id)
    assert reloaded.status is RunStatus.COMPLETED
    assert reloaded.status.is_terminal
    assert reloaded.answer == "160211449"
    assert reloaded.lease_owner is None
    assert reloaded.lease_expires_at is None


async def test_a_failed_run_keeps_its_error_code(store: PostgresRunStore) -> None:
    """The code is what a client switches on; the message is for a human (plan.md §2.1)."""
    run = await store.create(new_run())

    await store.finish(
        run.id,
        RunOutcome(
            status=RunStatus.FAILED,
            usage=RunUsage(),
            messages=(),
            error_code=ErrorCode.BUDGET_EXCEEDED,
            error_message="Run exceeded its step cap of 6 at step 6.",
        ),
    )

    reloaded = await store.load(run.id)
    assert reloaded.error_code is ErrorCode.BUDGET_EXCEEDED
    assert "step cap" in (reloaded.error_message or "")


# --- What a resuming worker sees -----------------------------------------------------


async def test_a_second_store_sees_exactly_what_the_first_committed(
    store: PostgresRunStore,
    session_factory: object,
) -> None:
    """
    The whole point, stated plainly.

    A different worker — here, a different store object with its own sessions — loads
    the run and knows precisely where to continue, because the committed rows are the
    truth and `next_step_idx` is derived from them rather than from a counter that could
    disagree.
    """
    run = await store.create(new_run())
    await store.commit_step(run.id, a_step(0), [Message(role="assistant", content=[])])
    await store.commit_step(run.id, a_step(1), [Message(role="assistant", content=[])])

    other_worker = PostgresRunStore(session_factory)  # type: ignore[arg-type]
    reloaded = await other_worker.load(run.id)

    assert reloaded.next_step_idx == 2
    assert reloaded.usage.steps == 2
    assert reloaded.status is RunStatus.RUNNING
