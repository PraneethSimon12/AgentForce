"""
Integration tests for the idempotency ledger.

These test the answer to the one question a crashed-and-resumed run has to ask: **did
this tool already run, and is it safe to run it again?** The interesting cases are all
failures of a system that looks like it is working.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.db.run_store import PostgresRunStore
from app.adapters.db.tool_ledger import PostgresToolLedger
from app.core.runtime.idempotency import InvocationAction, invocation_key
from app.core.runtime.state import NewRun, RunLimits
from app.core.tools.base import EffectClass

pytestmark = pytest.mark.integration


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


# --- The key -------------------------------------------------------------------------


def test_the_same_call_produces_the_same_key() -> None:
    run = uuid.uuid4()
    args = {"expression": "2 + 2", "precision": 2}

    assert invocation_key(run, 3, "calculator", args) == invocation_key(run, 3, "calculator", args)


def test_argument_order_does_not_change_the_key() -> None:
    """
    `sort_keys=True` earning its place.

    Python dicts preserve insertion order and the model emits JSON in whatever order it
    likes, so without canonical serialisation the *same* call could hash two ways — and
    the ledger would silently stop deduplicating while still appearing to work.
    """
    run = uuid.uuid4()

    assert invocation_key(run, 0, "calculator", {"a": 1, "b": 2}) == invocation_key(
        run, 0, "calculator", {"b": 2, "a": 1}
    )


def test_a_different_step_is_a_different_invocation() -> None:
    """The same tool called twice in one run is two invocations, not one replay."""
    run = uuid.uuid4()
    args = {"expression": "2 + 2"}

    assert invocation_key(run, 0, "calculator", args) != invocation_key(run, 1, "calculator", args)


def test_different_arguments_are_a_different_invocation() -> None:
    """A resumed step where the model changes its mind must genuinely re-execute."""
    run = uuid.uuid4()

    assert invocation_key(run, 0, "calculator", {"expression": "2+2"}) != invocation_key(
        run, 0, "calculator", {"expression": "3+3"}
    )


# --- begin / complete ----------------------------------------------------------------


async def test_a_fresh_invocation_says_execute(
    ledger: PostgresToolLedger, run_id: uuid.UUID
) -> None:
    key = invocation_key(run_id, 0, "calculator", {"expression": "2+2"})

    decision = await ledger.begin(run_id, 0, "calculator", EffectClass.READ_ONLY, key)

    assert decision.action is InvocationAction.EXECUTE


async def test_a_completed_invocation_is_replayed_not_re_executed(
    ledger: PostgresToolLedger, run_id: uuid.UUID
) -> None:
    """
    The t2-t3 window, fully closed.

    The tool ran, its result was recorded, and then the process died before the step was
    committed. On resume the step is redone — but the tool is not. The recorded result
    comes back instead, which is what makes "the tool executed exactly once" true across
    a crash.
    """
    key = invocation_key(run_id, 0, "send_invoice", {"customer": 42})
    await ledger.begin(run_id, 0, "send_invoice", EffectClass.UNSAFE, key)
    await ledger.complete(key, "invoice-7781 sent", is_error=False)

    decision = await ledger.begin(run_id, 0, "send_invoice", EffectClass.UNSAFE, key)

    assert decision.action is InvocationAction.REPLAY
    assert decision.result == "invoice-7781 sent"
    assert decision.is_error is False


async def test_a_recorded_failure_is_replayed_rather_than_retried(
    ledger: PostgresToolLedger, run_id: uuid.UUID
) -> None:
    """Retrying a *declared* failure spends money to get the same answer."""
    key = invocation_key(run_id, 0, "calculator", {"expression": "1/0"})
    await ledger.begin(run_id, 0, "calculator", EffectClass.READ_ONLY, key)
    await ledger.complete(key, "Division by zero.", is_error=True)

    decision = await ledger.begin(run_id, 0, "calculator", EffectClass.READ_ONLY, key)

    assert decision.action is InvocationAction.REPLAY
    assert decision.is_error is True


# --- The window that cannot be closed ------------------------------------------------


@pytest.mark.parametrize(
    ("effect_class", "expected"),
    [
        (EffectClass.READ_ONLY, InvocationAction.RE_EXECUTE),
        (EffectClass.IDEMPOTENT_WRITE, InvocationAction.RE_EXECUTE),
        (EffectClass.UNSAFE, InvocationAction.NEEDS_REVIEW),
    ],
)
async def test_a_pending_record_is_resolved_by_the_effect_class(
    ledger: PostgresToolLedger,
    run_id: uuid.UUID,
    effect_class: EffectClass,
    expected: InvocationAction,
) -> None:
    """
    The t1-t2 window: the tool may or may not have run, and nothing here can tell.

    A PENDING row left behind by a crash is genuinely ambiguous — the side effect
    happened outside this database, so no transaction covers it. The only party who can
    say whether replaying is acceptable is whoever wrote the tool, and they said so at
    definition time in a required field (D-004).
    """
    key = invocation_key(run_id, 0, "tool", {"n": 1})
    await ledger.begin(run_id, 0, "tool", effect_class, key)  # left PENDING: crash here

    decision = await ledger.begin(run_id, 0, "tool", effect_class, key)

    assert decision.action is expected


async def test_an_unsafe_tool_never_auto_retries(
    ledger: PostgresToolLedger, run_id: uuid.UUID
) -> None:
    """
    This is the behaviour, not a limitation.

    The alternatives for an interrupted UNSAFE tool are double-charging a customer or
    throwing away the run. Neither is ours to pick silently, so the run stops and a
    human resolves it.
    """
    key = invocation_key(run_id, 0, "charge_card", {"amount": 5000})
    await ledger.begin(run_id, 0, "charge_card", EffectClass.UNSAFE, key)

    for _ in range(3):
        decision = await ledger.begin(run_id, 0, "charge_card", EffectClass.UNSAFE, key)
        assert decision.action is InvocationAction.NEEDS_REVIEW


# --- Concurrency ---------------------------------------------------------------------


async def test_two_workers_racing_one_tool_call_produce_one_execution(
    session_factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> None:
    """
    Insert-first, not check-then-insert.

    A SELECT that finds nothing followed by an INSERT has a window, and two workers
    passing through it together both conclude they should execute — the exact failure
    the ledger exists to prevent. Letting `UNIQUE(idempotency_key)` arbitrate means the
    database decides atomically: one EXECUTE, everyone else reads the winner's row.
    """
    key = invocation_key(run_id, 0, "charge_card", {"amount": 5000})
    ledgers = [PostgresToolLedger(session_factory) for _ in range(10)]

    decisions = await asyncio.gather(
        *(led.begin(run_id, 0, "charge_card", EffectClass.IDEMPOTENT_WRITE, key) for led in ledgers)
    )

    executors = [d for d in decisions if d.action is InvocationAction.EXECUTE]
    assert len(executors) == 1
    # Everyone else saw the PENDING row and deferred to the effect class.
    assert all(d.action is InvocationAction.RE_EXECUTE for d in decisions if d not in executors)
