"""
The seams: every Protocol `core/` depends on, and no implementation of any of them.

`core/` contains no IO and imports nothing from `adapters/`. These Protocols are the
mechanism. The agent loop asks for something shaped like `LLMClient`; in production it
gets the Anthropic adapter, and in a unit test it gets `FakeLLM` replaying a scripted
list of responses. Neither the loop nor the test knows which. That is Dependency
Inversion, and the payoff is concrete: the v0 exit criterion is a green test suite with
no network, no Docker and no model weights, which is only reachable if nothing in
`core/` can reach the outside world.

`Protocol`, not `ABC`, and the difference matters. An ABC demands that implementations
inherit from it, which means `adapters/` would import `core/` *and* `core/` would define
the base class the adapter is built on — a two-way coupling. A Protocol is structural:
any class with the right methods satisfies it, checked statically by mypy, with no
inheritance and no import in that direction at all.

Not `@runtime_checkable`: that only verifies method *names* exist at runtime, never their
signatures, so it buys a false sense of safety in exchange for an `isinstance` check we
do not need. mypy --strict already checks the real thing, before the code runs.

Ports arrive with the phase that implements them (D-015). `RunStore` landed with v1;
`EventBus` arrives with v2's streaming, and `Retriever`/`Reranker` with v3.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from app.core.runtime.idempotency import InvocationDecision
from app.core.runtime.messages import Effort, LLMResponse, Message, ToolSchema
from app.core.runtime.state import NewRun, RunOutcome, RunRecord, StepRecord
from app.core.tools.base import EffectClass


class Clock(Protocol):
    """
    Everything time-dependent, in one injectable place.

    A port because time is IO in every way that matters for testing. Per-step timeouts,
    retry backoff and (in v1) lease expiry are all decisions made against a clock, and a
    test that has to wait 60 real seconds to prove a 60-second timeout fires is a test
    nobody runs. A fake clock makes those paths instant and deterministic.
    """

    def now(self) -> datetime:
        """Return the current time as an aware UTC datetime. Never naive."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Yield control for `seconds`. Must not block the event loop."""
        ...


class LLMClient(Protocol):
    """
    One request to a language model. The narrowest useful surface.

    Deliberately *not* on this interface:

    - **The model id.** An implementation is bound to one model at construction. The
      eval harness wanting a cheap judge alongside the expensive agent builds two
      clients, which is honest — a client is a configured thing, not a dispatcher.
    - **Retry.** The port raises a retryable or a non-retryable error and stops there.
      The loop owns the retry budget, because the budget is per *step* and per *run*,
      facts this object cannot see (D-009).
    - **Prompt caching.** `system` is a plain string; the adapter is what wraps it with
      `cache_control` before it goes on the wire. Caching is a property of the transport,
      not of the conversation, and core has no business knowing about a cache breakpoint.
    - **Streaming.** Arrives in v2 as a second method, which is an additive change. A
      Protocol can grow; the implementations simply have to keep up.
    """

    async def complete(
        self,
        *,
        messages: Sequence[Message],
        system: str,
        tools: Sequence[ToolSchema],
        max_tokens: int,
        effort: Effort,
    ) -> LLMResponse:
        """
        Send one request and return the translated response.

        Responsibility: exactly one round trip. Does NOT execute tools (the loop does),
        does NOT append to the conversation (the loop does), and does NOT decide whether
        to continue (the policy does).

        Preconditions: `messages` alternates sensibly and ends with a user turn; every
        `tool_result` block in it carries a `tool_use_id` matching an earlier
        `tool_use`. `tools` must be ordered deterministically — the tool list is part of
        the cache prefix, so reordering it silently costs a cache hit on every step.

        Postcondition: the returned `stop_reason` has been read from the provider, never
        inferred from the content. A response with no text is a legitimate outcome, not
        an error.

        Raises:
            LLMTransportError: the call failed in a way that may succeed on retry.
            LLMRequestError: the call failed in a way that will not.
        """
        ...


class RunStore(Protocol):
    """
    Durable run state. Postgres in production, and the reason a crash is survivable.

    The contract that matters is not "save things" — it is **one committed row per
    completed step, and nothing half-written**. Every method here either fully happens
    or fully does not, because the thing on the other side of a failure is a different
    worker trying to work out what already ran.

    Deliberately *not* on this interface: anything per-token. Tokens go to Redis; the
    database gets one row per step (D-003). Confusing the two turns a two-second answer
    into a two-hundred-write transaction storm.
    """

    async def create(self, spec: NewRun) -> RunRecord:
        """
        Record a run before it starts, and return it in QUEUED.

        Idempotent on `(tenant_id, idempotency_key)`: replaying a creation with the same
        key returns the original run rather than starting a second one. That is what
        makes a client's retry of a timed-out POST safe, and it is enforced by a unique
        constraint rather than a read-then-write, which would race.
        """
        ...

    async def load(self, run_id: uuid.UUID) -> RunRecord:
        """
        Load a run with its committed steps and its full conversation, in order.

        Postcondition: the returned `messages` are byte-identical to what was committed
        — blocks rebuilt through `block_from_dict`, not re-rendered.

        Raises: RunNotFound.
        """
        ...

    async def commit_step(
        self,
        run_id: uuid.UUID,
        step: StepRecord,
        messages: Sequence[Message],
    ) -> None:
        """
        Commit one completed step and the messages it produced, atomically.

        This single method is the durability guarantee. The step row, its messages and
        the run's usage counters go in **one transaction**: a crash on the next line
        loses nothing, and a crash during it leaves no partial step behind.

        Preconditions: `step.idx` equals the run's `next_step_idx`.
        Raises: StepAlreadyCommitted if that index is already present — which is not an
            error condition so much as the answer to "did I already do this?" after a
            crash. `UNIQUE(run_id, idx)` is what makes the question answerable at all.
        """
        ...

    async def finish(self, run_id: uuid.UUID, outcome: RunOutcome) -> None:
        """
        Write the terminal state: status, answer or error, and the completion time.

        Separate from `commit_step` because a run can end without a step succeeding —
        a budget refusal happens *before* a request is made, so there is no step to
        attach it to.
        """
        ...


class ToolLedger(Protocol):
    """
    The record of which tool calls have been attempted, and how they turned out.

    Separate from `RunStore` because it is written on a different schedule: a ledger row
    is committed *before* the tool runs and a step row *after*, and a port that mixed the
    two would invite someone to write them together — which would close the wrong window
    and reopen the dangerous one.
    """

    async def begin(
        self,
        run_id: uuid.UUID,
        step_idx: int,
        tool_name: str,
        effect_class: EffectClass,
        key: str,
    ) -> InvocationDecision:
        """
        Claim this invocation, or report what already happened to it.

        Committed before the tool is executed, so a crash during execution leaves
        evidence the attempt existed. The `UNIQUE(idempotency_key)` constraint is what
        makes the claim atomic: two workers racing the same call produce one insert and
        one conflict, and the loser reads the winner's row rather than executing.

        Postcondition: on EXECUTE, a PENDING row exists and is committed.
        """
        ...

    async def complete(self, key: str, result: str, *, is_error: bool) -> None:
        """
        Record the outcome, in its own transaction, immediately after execution.

        Its own transaction and not the step's, deliberately. Completing here closes the
        t2-t3 window entirely: a crash after this point finds SUCCEEDED on resume and
        replays the recorded result instead of executing again. Deferring it to the step
        commit would leave that window open and send more crashes down the ambiguous
        PENDING path — including UNSAFE tools, which would then stop for human review
        when they did not need to (D-021).
        """
        ...
