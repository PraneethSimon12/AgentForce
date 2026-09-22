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
`TaskQueue` with v1.7's durable tools; `EventBus` arrives with v2's streaming, and
`Retriever`/`Reranker` with v3.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from app.core.runtime.idempotency import InvocationDecision, InvocationOutcome
from app.core.runtime.messages import Effort, LLMResponse, Message, ToolSchema
from app.core.runtime.state import NewRun, RunOutcome, RunRecord, RunStatus, StepRecord
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

    async def record_retry(self, run_id: uuid.UUID) -> int:
        """
        Charge the run one retry and return the new total.

        Durable because a per-step counter is reset by the very crash it is meant to
        bound. Without a row, a process that dies during backoff and resumes would get a
        fresh allowance every time, and a crash loop would retry forever (D-009).
        """
        ...

    async def claim(self, run_id: uuid.UUID, owner: str, ttl_seconds: int) -> RunRecord | None:
        """
        Take the lease on a run, or return None because someone else holds it.

        Must be atomic. A check that the lease is free followed by a separate write
        taking it has a window in between, and under concurrency several workers pass
        through that window together — which is two workers driving one run, each
        believing it is alone. Returning None rather than raising, because a recovery
        scan finding a run already in progress is ordinary.
        """
        ...

    async def claim_next_reclaimable(self, owner: str, ttl_seconds: int) -> RunRecord | None:
        """
        Find any run nobody is working on and take it. The crash-recovery scan.

        Oldest first, so a run abandoned by a crashed worker is picked up before newer
        work rather than starving behind it.
        """
        ...

    async def renew(self, run_id: uuid.UUID, owner: str, ttl_seconds: int) -> bool:
        """
        Extend the lease, but only if `owner` still holds it.

        Returns False when it does not, and that answer is the point: a worker which
        paused long enough for its lease to expire has *already* had the run taken away,
        and must stop rather than write steps alongside its replacement.
        """
        ...

    async def release(
        self, run_id: uuid.UUID, owner: str, *, status: RunStatus = RunStatus.PAUSED
    ) -> None:
        """
        Give up the lease deliberately, leaving the run resumable.

        The clean counterpart to a crash: a worker shutting down releases, so the run is
        picked up immediately instead of after the TTL expires.
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

    async def lookup(self, key: str) -> InvocationOutcome | None:
        """
        Read what this invocation's row says, without claiming it. None if there is none.

        The read-only counterpart to `begin`, and the distinction is load-bearing.
        `begin` *claims*: it commits a PENDING row as part of asking, which is right when
        the caller is about to execute and wrong when the caller is waiting for someone
        else to (D-023). A loop polling a durable tool that is running in a Celery worker
        must be able to ask "has it finished?" without inserting anything and without
        being told to execute.

        Postcondition: nothing is written. Calling this a thousand times changes nothing.
        """
        ...

    async def needs_review(self, key: str, reason: str) -> None:
        """
        Close an invocation nobody may run again, because replaying it is unsafe.

        Written by whichever attempt found a PENDING row for an UNSAFE tool. It is the
        only way a redelivered Celery task can tell the waiting loop "stop" — the two
        processes share nothing else (D-023), and a loop that kept polling would report
        a slow tool where the truth is an unresolved one.

        Postcondition: the row is terminal. No further attempt will execute this call.
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


class TaskQueue(Protocol):
    """
    Handing work to a process that is not this one. Celery in production (D-023).

    One method, and the narrowness is deliberate. Everything the queue might otherwise
    be asked to do — track the task, return its result, tell you whether it finished —
    is already the ledger's job, and a port that offered both would invite a caller to
    read the result from the wrong one. The queue's entire contract is *delivery*; the
    ledger's is *what happened*.

    Deliberately not here: a result handle, a task id, a cancel. A task id would be a
    second identity for something the idempotency key already identifies, and the thing
    a caller wants to cancel is the run, which it can already do.
    """

    async def enqueue_tool(
        self,
        run_id: uuid.UUID,
        step_idx: int,
        tool_name: str,
        args: Mapping[str, Any],
        key: str,
    ) -> None:
        """
        Deliver one tool invocation to a worker, at least once.

        `key` is the idempotency key the worker will write to the ledger, computed by the
        caller rather than the worker so that a duplicate delivery of the same logical
        call carries the same key. Recomputing it worker-side would work only for as long
        as the two computations agreed, which is the drift this project keeps designing
        out.

        At-least-once, explicitly: this may be delivered twice, and a worker that dies
        mid-task will see it again. The guarantee this port makes is that the task is not
        silently dropped; making a second delivery harmless is the ledger's job, not
        this one's.

        Postcondition: the task is durably queued — the broker has accepted it. Returning
        does NOT mean the tool has run, or started.

        Raises: TaskQueueUnavailable if the broker could not be reached, which is
            retryable at the step level like any other transport failure.
        """
        ...
