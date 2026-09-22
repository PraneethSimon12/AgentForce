"""
The ReAct loop: model → tool → result → model, until an answer or a limit.

Hand-written rather than the SDK's `tool_runner` (D-002), because the loop is where
every requirement of this project lives: a durable checkpoint between steps, a lease so
two workers cannot drive one run, tool calls guarded by an idempotency ledger, a per-step
timeout, bounded retries, and resumption from Postgres on a different machine. None of
those are hooks the runner exposes.

**There is one loop, and it is always durable.** Unit tests supply in-memory
implementations of `RunStore` and `ToolLedger` rather than taking a different code path,
because a second, simpler loop for tests would be a second implementation of the most
important control flow in the project and the two would drift.

The protocol rules this file exists to get right (CLAUDE.md §8):

1. **`stop_reason` is read before `content`, every time.** A refusal is an HTTP 200 with
   an empty content list, and a `max_tokens` truncation is a valid-looking response with
   a broken intent. Both are indistinguishable from success if you read the blocks first.
2. **The assistant turn is appended verbatim.** Raw blocks, including opaque thinking
   blocks, so they round-trip byte-identically through Postgres (D-014).
3. **All tool results from one turn go back in a single user message.** Splitting them
   silently teaches the model to stop calling tools in parallel. Nothing errors.
4. **Every tool_use gets a result, including a failed one**, carrying its `tool_use_id`
   and `is_error=True`. A dropped result leaves the model with an unanswered question.

And the durability rules:

5. **Nothing is believed until it is committed.** Progress is the number of committed
   step rows, never a counter the process is holding.
6. **The ledger is consulted before every tool call and completed immediately after**,
   before the step commit, which is what makes a tool that ran once not run twice
   (D-021). That sequence lives in `core/tools/execution.py`, because a Celery worker
   executing a DURABLE tool has to follow exactly the same one.
7. **The lease is renewed at every step boundary.** A worker that has lost its lease
   stops there rather than writing alongside its replacement.
8. **Retries are bounded twice and counted deliberately** (D-009): per step in memory,
   per run on the row, with the SDK's own two retries underneath. Knowing there are
   three layers is the point — multiply them without noticing and one user action
   becomes eighteen attempts.
9. **A DURABLE tool is dispatched, never executed here** (D-023), and the loop enqueues
   it only when the ledger has no row for it. A row means a worker already owns the
   call, and re-enqueueing would duplicate the redelivery the broker already promises.
   The loop waits on the ledger; the broker owns delivery; the ledger owns the outcome.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.core.ports import Clock, LLMClient, RunStore, TaskQueue, ToolLedger
from app.core.runtime.errors import (
    LLMTransportError,
    QueueNotConfigured,
    RetriesExhausted,
    RunNotResumable,
    StepAlreadyCommitted,
    StepTimeout,
    TaskQueueUnavailable,
    ToolCrashed,
    ToolInputInvalid,
    ToolNotFound,
)
from app.core.runtime.idempotency import (
    InvocationOutcome,
    InvocationStatus,
    invocation_key,
)
from app.core.runtime.messages import (
    ContentBlock,
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from app.core.runtime.policy import BudgetExceeded, LoopPolicy
from app.core.runtime.retry import BackoffPolicy
from app.core.runtime.state import (
    ErrorCode,
    RunLimits,
    RunOutcome,
    RunRecord,
    RunStatus,
    RunUsage,
    StepRecord,
    ToolCallRecord,
)
from app.core.tools.base import ExecutionMode, ToolSpec
from app.core.tools.execution import execute_invocation
from app.core.tools.registry import ToolRegistry

# How often the loop asks the ledger whether a durable tool has finished (D-023). It
# starts eager because most durable tools are seconds, not minutes, and backs off so a
# genuinely long one does not cost a query every 100ms for ten minutes. The ceiling is
# the latency this adds to a fast-but-durable tool, which is the number to move if that
# ever shows up in a measurement.
POLL_MIN_SECONDS = 0.1
POLL_MAX_SECONDS = 2.0

# The waiting stops slightly before the step's hard deadline, so a tool that is still
# running lands in `_ToolInFlight` rather than in `asyncio.timeout`. Both stop the step;
# only one of them says *why*, and the difference decides whether the run burns its
# retries re-asking the model or pauses and comes back to a finished tool.
#
# The reserve is the larger of a fraction and a floor, and it needs both. The fraction
# keeps it proportionate at a sixty-second budget; the floor exists because event-loop
# wakeup jitter is *absolute* — a sleep can overshoot by a timer tick whatever the
# budget is, and on Windows that tick is ~16ms. A pure fraction leaves a sub-second
# budget with less margin than one scheduling delay, which is how this was first found.
INFLIGHT_RESERVE_FRACTION = 0.05
INFLIGHT_RESERVE_FLOOR_SECONDS = 0.1


class _ToolInFlight(Exception):
    """
    Internal signal: a durable tool is still running and the step ran out of time to wait.

    Not a failure of anything. The tool is healthy, the worker is working, and the result
    will land in the ledger — the step simply is not allowed to wait for it. Private for
    the same reason as `_NeedsReview`: it never escapes `run()`, where it becomes a
    PAUSED run with `TOOL_PENDING`.

    Deliberately not retryable. Retrying would re-issue the model call to rediscover a
    fact the ledger already knows, at the cost of a step's worth of tokens per attempt.
    """

    def __init__(self, tool_name: str, step_idx: int) -> None:
        super().__init__(
            f"Durable tool {tool_name!r} was still running when step {step_idx} ran out "
            f"of time to wait. Its result will be recorded; resume the run to collect it."
        )
        self.tool_name = tool_name
        self.step_idx = step_idx


class _NeedsReview(Exception):
    """
    Internal signal: an UNSAFE tool was interrupted and a human must resolve it.

    Private because it never escapes `run()` — it becomes a terminal outcome with
    `NEEDS_REVIEW`. It exists as an exception only because it has to unwind out of the
    middle of tool execution. Deliberately **not** retryable: retrying is the one thing
    that must not happen here.
    """

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            f"Tool {tool_name!r} is UNSAFE and was interrupted mid-flight. "
            f"Replaying it could double its effect, so the run stops for review."
        )
        self.tool_name = tool_name


class _ToolBug(Exception):
    """
    An unexpected exception escaped a tool handler.

    Wrapped rather than caught at the top of the step, so the broad `except Exception`
    lives at the one place it is justified — around a third party's handler — instead of
    around the whole step, where it would also swallow bugs in the loop itself and
    report them as tool failures. Narrowing the blast radius of a broad catch is most of
    what makes it acceptable at all.
    """

    def __init__(self, cause: Exception) -> None:
        super().__init__(f"A tool raised {type(cause).__name__}: {cause}")
        self.cause = cause


@dataclass(frozen=True, slots=True)
class _Attempt:
    """One successful pass at a step: what the model said and what the tools returned."""

    response: LLMResponse
    results: list[ToolResultBlock]
    records: list[ToolCallRecord]
    attempt: int


class AgentLoop:
    """
    Drives one run from wherever it currently is to a terminal state.

    Responsibility: control flow, the block protocol, and the durability sequence. Does
    NOT choose tools (the model does), does NOT decide the limits (the policy does, from
    limits frozen onto the run), does NOT talk to a provider (the client does), and does
    NOT know what a table is (the store does).

    Holds no per-run state. Every run's state lives in the store and in local variables
    inside `run()`, which is what makes one instance safe to share across concurrent
    runs on one worker.
    """

    def __init__(
        self,
        *,
        llm: LLMClient,
        registry: ToolRegistry,
        store: RunStore,
        ledger: ToolLedger,
        clock: Clock,
        load_prompt: Callable[[str, int], str],
        queue: TaskQueue | None = None,
        backoff: BackoffPolicy | None = None,
        rng: Callable[[], float] = random.random,
        lease_ttl_seconds: int = 60,
    ) -> None:
        """
        Wire one loop.

        Raises: QueueNotConfigured if the registry contains a DURABLE tool and no queue
            was supplied. Checked here rather than at dispatch because the registry is
            fixed at construction, so the answer is already knowable — and the difference
            between the two is a process that refuses to start and a run that dies
            halfway through, hours later, on the one step that happened to need it.
        """
        self._llm = llm
        self._registry = registry
        self._store = store
        self._ledger = ledger
        self._clock = clock
        self._load_prompt = load_prompt
        self._queue = queue
        self._backoff = backoff or BackoffPolicy()
        # Injected so the jitter is deterministic under test. The only nondeterminism in
        # the retry path, kept visible rather than reached for inside the policy.
        self._rng = rng
        self._lease_ttl = lease_ttl_seconds

        if queue is None:
            unroutable = [
                name
                for name in registry.names()
                if registry.get(name).execution is ExecutionMode.DURABLE
            ]
            if unroutable:
                raise QueueNotConfigured(
                    f"No TaskQueue was supplied, but these tools are DURABLE and can only "
                    f"run on a worker: {', '.join(unroutable)}."
                )

    async def run(self, run_id: uuid.UUID, *, owner: str) -> RunOutcome:
        """
        Claim a run and drive it to a terminal state, committing every step.

        The same entry point for a fresh run and a resumed one. There is no separate
        "resume" path, because a fresh run is simply one whose committed step count is
        zero — and a second code path for resumption is a path that only executes after
        a crash, which is the worst possible place for untested code.

        Preconditions: the run exists and is not terminal.
        Postcondition: the returned outcome is terminal and has been persisted.
        Raises: RunNotResumable if the run is terminal, if another worker holds a live
            lease, or if this worker loses its lease mid-run.
        """
        record = await self._store.claim(run_id, owner, self._lease_ttl)
        if record is None:
            raise RunNotResumable(f"Run {run_id} is terminal or leased by another worker.")

        limits = record.limits
        policy = LoopPolicy(limits)
        system_prompt = self._load_prompt(record.prompt_name, record.prompt_version)
        messages = self._conversation(record)
        pending: list[Message] = [] if record.messages else list(messages)
        usage = record.usage
        steps = list(record.steps)

        while True:
            try:
                policy.check(usage)
            except BudgetExceeded as exc:
                return await self._fail(
                    run_id, ErrorCode.BUDGET_EXCEEDED, str(exc), usage, messages, steps
                )

            step_idx = len(steps)
            try:
                attempt = await self._attempt_step(
                    run_id=run_id,
                    step_idx=step_idx,
                    messages=messages,
                    system_prompt=system_prompt,
                    max_tokens=policy.max_tokens_for_next_step(usage),
                    record=record,
                    limits=limits,
                )
            except RetriesExhausted as exc:
                if exc.run_budget_exhausted:
                    # The run has spent its whole allowance. Terminal: something is
                    # durably wrong and resuming would only keep paying to discover it.
                    return await self._fail(
                        run_id, ErrorCode.STEP_FAILED, str(exc), usage, messages, steps
                    )
                # Only this step's attempts ran out. A provider outage is not permanent,
                # so the run is left PAUSED and resumable rather than killed — the
                # durable per-run counter is what stops that becoming an infinite loop.
                return await self._pause(
                    run_id, owner, ErrorCode.STEP_FAILED, str(exc), usage, messages, steps
                )
            except _ToolInFlight as exc:
                # Nothing is wrong, so nothing is failed and no retry is charged. The
                # tool outlives this process; the run comes back to a finished ledger row
                # and replays it. The step's model call is lost and paid for again on
                # resume — one call, which is the price of not holding a lease open for
                # an unbounded time (D-023).
                return await self._pause(
                    run_id, owner, ErrorCode.TOOL_PENDING, str(exc), usage, messages, steps
                )
            except _NeedsReview as exc:
                return await self._fail(
                    run_id, ErrorCode.NEEDS_REVIEW, str(exc), usage, messages, steps
                )
            except _ToolBug as exc:
                # A bug in a tool is a recorded failed run, not a dead worker taking
                # every other run on the process down with it.
                return await self._fail(
                    run_id, ErrorCode.TOOL_FAILED, str(exc), usage, messages, steps
                )

            response = attempt.response
            usage = usage.plus(response.usage)

            # Rule 1: stop_reason first. Everything below this line may read content.
            if response.stop_reason == "refusal":
                category = response.stop_details.category if response.stop_details else None
                return await self._fail(
                    run_id,
                    ErrorCode.UPSTREAM_REFUSAL,
                    f"The model declined to continue (category: {category}).",
                    usage,
                    messages,
                    steps,
                )

            if response.stop_reason in ("max_tokens", "pause_turn"):
                return await self._fail(
                    run_id,
                    ErrorCode.UPSTREAM_TRUNCATED,
                    f"The model stopped with {response.stop_reason!r}; its output is "
                    f"incomplete and may contain a partial tool call.",
                    usage,
                    messages,
                    steps,
                )

            if response.stop_reason == "tool_use" and not attempt.records:
                # "tool_use" with no tool_use block. Appending an empty user message
                # would produce a malformed next request, so the run stops instead.
                return await self._fail(
                    run_id,
                    ErrorCode.UPSTREAM_TRUNCATED,
                    "The model signalled a tool call but emitted no tool_use block.",
                    usage,
                    messages,
                    steps,
                )

            # Rule 2: verbatim, opaque blocks and all.
            assistant_turn = Message(role="assistant", content=list(response.content))
            messages.append(assistant_turn)
            pending.append(assistant_turn)

            if response.stop_reason == "tool_use":
                # Rule 3: one user message, every result inside it.
                results_turn = Message(role="user", content=list(attempt.results))
                messages.append(results_turn)
                pending.append(results_turn)

                step = StepRecord(
                    step_idx,
                    response.stop_reason,
                    response.usage,
                    tuple(attempt.records),
                    attempt=attempt.attempt,
                )
                await self._commit(run_id, step, pending, owner)
                steps.append(step)
                pending = []
                continue

            # end_turn or stop_sequence: the model is done.
            step = StepRecord(
                step_idx, response.stop_reason, response.usage, attempt=attempt.attempt
            )
            await self._commit(run_id, step, pending, owner)
            steps.append(step)

            outcome = RunOutcome(
                status=RunStatus.COMPLETED,
                usage=usage,
                messages=tuple(messages),
                steps=tuple(steps),
                answer=_answer_from(response.content),
            )
            await self._store.finish(run_id, outcome)
            return outcome

    # --- one step, with a deadline and a retry budget --------------------------------

    async def _attempt_step(
        self,
        *,
        run_id: uuid.UUID,
        step_idx: int,
        messages: Sequence[Message],
        system_prompt: str,
        max_tokens: int,
        record: RunRecord,
        limits: RunLimits,
    ) -> _Attempt:
        """
        Make one model call and run whatever tools it asks for, retrying transient failure.

        The timeout covers **both** phases together, because "a step took too long" is
        one budget and splitting it into two would let a step take twice as long as
        configured while each half stayed inside its own limit.

        Retrying the whole step means the model is asked again, which costs tokens. That
        is acceptable precisely because of the ledger: if the model repeats the same call
        the recorded result is replayed rather than the tool re-executed, and if it makes
        a different call then executing it is correct because it *is* a different action.

        Retryable: transport failures, timeouts, and a broker that could not be reached
        (the durable tool provably did not start, so asking again risks nothing). Not
        retryable: `LLMRequestError` (a 400 never becomes a 200, and retrying spends
        money slowly while hiding the bug), `_NeedsReview`, `_ToolInFlight`, and
        unexpected tool exceptions.

        Raises: RetriesExhausted when either bound is reached; anything non-retryable,
            untouched.
        """
        attempt = 1
        while True:
            # When a durable tool must stop being waited for. Monotonic, and re-taken on
            # every attempt because each attempt gets the whole budget. The event loop's
            # clock rather than the injected `Clock` because this measures elapsed time
            # inside one process, where a wall clock stepping backwards over NTP would
            # silently extend the deadline.
            reserve = max(
                limits.step_timeout_seconds * INFLIGHT_RESERVE_FRACTION,
                INFLIGHT_RESERVE_FLOOR_SECONDS,
            )
            wait_deadline = asyncio.get_running_loop().time() + max(
                limits.step_timeout_seconds - reserve, 0.0
            )
            try:
                async with asyncio.timeout(limits.step_timeout_seconds):
                    response = await self._llm.complete(
                        messages=messages,
                        system=system_prompt,
                        tools=self._registry.schemas(),
                        max_tokens=max_tokens,
                        effort=record.effort,
                    )
                    results: list[ToolResultBlock] = []
                    records: list[ToolCallRecord] = []
                    if response.stop_reason == "tool_use":
                        requested = [b for b in response.content if isinstance(b, ToolUseBlock)]
                        results, records = await self._execute_all(
                            run_id, step_idx, requested, wait_deadline
                        )
                return _Attempt(response, results, records, attempt)

            except TimeoutError as exc:
                failure: Exception = StepTimeout(step_idx, limits.step_timeout_seconds)
                failure.__cause__ = exc
            except (LLMTransportError, TaskQueueUnavailable) as exc:
                failure = exc

            attempt += 1
            if attempt > limits.max_step_attempts:
                raise RetriesExhausted(
                    f"Step {step_idx} failed {limits.max_step_attempts} times; "
                    f"last error: {failure}",
                    last_error=str(failure),
                )

            # The durable half of the budget. Recorded *before* sleeping, so a crash
            # during the backoff still costs the run a retry — otherwise a crash loop
            # would get an unlimited allowance by dying before it paid for one.
            total = await self._store.record_retry(run_id)
            if total > limits.max_run_retries:
                raise RetriesExhausted(
                    f"Run exhausted its {limits.max_run_retries} retries; last error: {failure}",
                    last_error=str(failure),
                    # This bound ends the run. The per-step bound only pauses it — the
                    # difference is whether the allowance belongs to one attempt or to
                    # the whole run.
                    run_budget_exhausted=True,
                )

            await self._clock.sleep(self._backoff.delay_for(attempt, self._rng()))

    # --- durability ------------------------------------------------------------------

    async def _commit(
        self,
        run_id: uuid.UUID,
        step: StepRecord,
        messages: Sequence[Message],
        owner: str,
    ) -> None:
        """
        Make one step durable, then confirm we are still allowed to continue.

        `StepAlreadyCommitted` is swallowed on purpose. It means a previous attempt at
        this exact step got its commit in before dying, so the work is already recorded
        and doing it again would be the duplicate the constraint exists to refuse. This
        is the one place where a database error is an answer rather than a problem.

        The lease renewal comes *after* the commit, at the step boundary, so a worker
        which has lost its lease finds out before issuing another model call rather than
        after. Renewal here rather than on a background timer means no extra task to
        leak, at the cost of requiring the TTL to exceed the per-step timeout — which is
        why those two settings are related and neither should be tuned alone.
        """
        try:
            await self._store.commit_step(run_id, step, messages)
        except StepAlreadyCommitted:
            pass

        if not await self._store.renew(run_id, owner, self._lease_ttl):
            raise RunNotResumable(
                f"Lost the lease on run {run_id} — another worker has taken it over."
            )

    async def _fail(
        self,
        run_id: uuid.UUID,
        code: ErrorCode,
        message: str,
        usage: RunUsage,
        messages: Sequence[Message],
        steps: Sequence[StepRecord],
    ) -> RunOutcome:
        """Persist a terminal failure that still carries the conversation and the trail."""
        outcome = RunOutcome(
            status=RunStatus.FAILED,
            usage=usage,
            messages=tuple(messages),
            steps=tuple(steps),
            error_code=code,
            error_message=message,
        )
        await self._store.finish(run_id, outcome)
        return outcome

    async def _pause(
        self,
        run_id: uuid.UUID,
        owner: str,
        code: ErrorCode,
        message: str,
        usage: RunUsage,
        messages: Sequence[Message],
        steps: Sequence[StepRecord],
    ) -> RunOutcome:
        """
        Stop without killing the run, releasing the lease so someone else can retry.

        The distinction from `_fail` is whether the cause could plausibly go away. A
        refusal, a truncation, a blown budget and a buggy tool will all happen again;
        a provider outage will not. Marking the second kind terminal would mean a
        five-minute upstream blip permanently destroying every run in flight.
        """
        await self._store.release(run_id, owner, status=RunStatus.PAUSED)
        return RunOutcome(
            status=RunStatus.PAUSED,
            usage=usage,
            messages=tuple(messages),
            steps=tuple(steps),
            error_code=code,
            error_message=message,
        )

    def _conversation(self, record: RunRecord) -> list[Message]:
        """
        The messages to send, either replayed from the store or freshly opened.

        A run that has committed nothing has no stored messages, so the opening user
        message is derived from `runs.input` — deterministically, so a crash before the
        first commit rebuilds exactly the same message rather than losing it.
        """
        if record.messages:
            return list(record.messages)
        query = record.input.get("query", "")
        return [Message(role="user", content=[TextBlock(text=str(query))])]

    # --- tools -----------------------------------------------------------------------

    async def _execute_all(
        self,
        run_id: uuid.UUID,
        step_idx: int,
        requested: Sequence[ToolUseBlock],
        wait_deadline: float,
    ) -> tuple[list[ToolResultBlock], list[ToolCallRecord]]:
        """
        Run every tool the model asked for, in the order it asked.

        Sequential, not concurrent, and that is a decision rather than an oversight: the
        inline tools complete in microseconds, so `gather` would buy nothing measurable
        while complicating both error attribution and the ledger's ordering. The
        protocol-level support for parallel calls — several tool_use blocks in one turn,
        all results in one user message — is here and tested; only the execution is
        serial, and it becomes `gather` when a tool is slow enough to show up in a
        measurement.

        DURABLE tools are the case that will eventually force that change (D-023): two
        of them in one turn wait one after the other, so the step spends the sum of their
        durations against a budget sized for one. They share `wait_deadline` rather than
        each getting the step's full budget, which at least makes the total bounded — but
        the honest fix is concurrency, and it is the first thing to do when a real step
        dispatches more than one.
        """
        results: list[ToolResultBlock] = []
        records: list[ToolCallRecord] = []
        for block in requested:
            result, record = await self._execute_one(run_id, step_idx, block, wait_deadline)
            results.append(result)
            records.append(record)
        return results, records

    async def _execute_one(
        self, run_id: uuid.UUID, step_idx: int, block: ToolUseBlock, wait_deadline: float
    ) -> tuple[ToolResultBlock, ToolCallRecord]:
        """
        Run one tool call through the ledger, or replay what already happened to it.

        Lookup and validation happen *before* the ledger is touched: a call naming a
        tool that does not exist, or carrying arguments that do not validate, never
        executed and never will, so recording an invocation for it would be recording an
        attempt that cannot happen.

        Three failures are recoverable and come back with `is_error=True` — unknown
        tool, invalid arguments, and a tool that declared it could not succeed. In all
        three the model reads the reason and tries something else, which turns a dead run
        into one more step. Anything else propagates, because that is the line between
        "the tool failed" and "the tool is broken".

        Raises: _NeedsReview for an interrupted UNSAFE tool; _ToolInFlight when a durable
            tool outlasts the step's patience; _ToolBug when an unexpected exception
            escapes an inline handler.
        """
        # Validated on this side of the branch for both modes. Shipping bad arguments to
        # a worker just to have them rejected there costs a queue round trip to produce
        # an error the model could have had immediately, and it strands a
        # ValidationError in a process whose only channel back is the ledger. The inline
        # path then validates a second time inside `execute_invocation` — a pure
        # function over a small dict, which is a cheaper price than two entry points
        # into the protocol.
        try:
            spec = self._registry.get(block.name)
            spec.validate_input(block.input)
        except ToolInputInvalid as exc:
            return _error_result(block, exc.detail)
        except ToolNotFound as exc:
            return _error_result(block, str(exc))

        if spec.execution is ExecutionMode.DURABLE:
            return await self._execute_durable(run_id, step_idx, block, spec, wait_deadline)

        try:
            outcome = await execute_invocation(
                registry=self._registry,
                ledger=self._ledger,
                run_id=run_id,
                step_idx=step_idx,
                tool_name=block.name,
                args=block.input,
                key=invocation_key(run_id, step_idx, block.name, block.input),
            )
        except ToolCrashed as exc:
            # The ledger row is already closed; what is left is the policy question, and
            # inline the answer is that a bug in a tool fails the run rather than being
            # fed back to the model as though it were an ordinary tool failure.
            raise _ToolBug(exc.cause) from exc

        return self._result_from(block, outcome)

    async def _execute_durable(
        self,
        run_id: uuid.UUID,
        step_idx: int,
        block: ToolUseBlock,
        spec: ToolSpec[Any],
        wait_deadline: float,
    ) -> tuple[ToolResultBlock, ToolCallRecord]:
        """
        Hand one tool call to a worker and wait for the ledger to say what happened.

        Responsibility: dispatch and wait. Does NOT execute the tool (a worker does),
        does NOT write the ledger row (the worker does, as part of executing), and does
        NOT retry a delivery (the broker does). Those three sentences are the whole of
        D-023: each mechanism has exactly one owner, and the ledger row is the only thing
        the two processes share.

        The dispatch rule is the subtle part. **Enqueue only when there is no row at
        all.** A row means some worker has already claimed this invocation — running it
        now, or dead and awaiting redelivery — and enqueueing again would put a second
        task alongside a delivery the broker has already promised to repeat. That is how
        you get two of everything: not from the queue failing, but from us duplicating
        what it already guarantees.

        A resumed run therefore lands correctly without a special case. No row: the
        earlier dispatch never happened, so dispatch now. A PENDING row: someone owns it,
        wait. A terminal row: the tool ran while this run was dead, which is the entire
        point of the mode.

        Raises: _NeedsReview if a redelivered attempt found an UNSAFE call ambiguous;
            _ToolInFlight if the step's patience runs out first; TaskQueueUnavailable if
            the broker cannot be reached, which is retryable because the tool provably
            never started.
        """
        if self._queue is None:
            # Unreachable: the constructor refuses a registry with a DURABLE tool and no
            # queue. Stated rather than asserted so the impossible case has a named
            # error instead of an AttributeError, and so mypy sees the narrowing.
            raise QueueNotConfigured(f"Tool {spec.name!r} is DURABLE but no queue is configured.")

        key = invocation_key(run_id, step_idx, block.name, block.input)
        outcome = await self._ledger.lookup(key)
        if outcome is None:
            await self._queue.enqueue_tool(run_id, step_idx, block.name, block.input, key)

        interval = POLL_MIN_SECONDS
        while outcome is None or not outcome.is_terminal:
            # Clamped to what is left, never just `interval`. A backed-off poll that
            # sleeps past its own deadline wakes up inside the hard `asyncio.timeout`
            # instead, and the step is then reported as a generic timeout and retried —
            # which is exactly the wasted model call this path exists to avoid.
            remaining = wait_deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise _ToolInFlight(block.name, step_idx)
            await self._clock.sleep(min(interval, remaining))
            interval = min(interval * 2, POLL_MAX_SECONDS)
            outcome = await self._ledger.lookup(key)

        return self._result_from(block, outcome)

    def _result_from(
        self, block: ToolUseBlock, outcome: InvocationOutcome
    ) -> tuple[ToolResultBlock, ToolCallRecord]:
        """
        Turn a finished ledger row into the blocks the model and the step record get.

        Shared by both execution modes, which is the point: whether the tool ran in this
        process or in a worker, what the model sees is derived from the same row by the
        same rules, so a durable tool cannot accidentally present differently.

        NEEDS_REVIEW is not a result and must not be handed to the model as one. It was
        written because replaying an UNSAFE call could double its effect; feeding that
        back as an ordinary failed tool_result would let the model cheerfully try again,
        which is the one thing the status exists to prevent.
        """
        if outcome.status is InvocationStatus.NEEDS_REVIEW:
            raise _NeedsReview(block.name)
        recorded = outcome.result or ""
        if outcome.is_error:
            return _error_result(block, recorded)
        return _ok_result(block, recorded)


def _ok_result(block: ToolUseBlock, output: str) -> tuple[ToolResultBlock, ToolCallRecord]:
    return (
        ToolResultBlock(tool_use_id=block.id, content=output),
        ToolCallRecord(tool_use_id=block.id, name=block.name, ok=True),
    )


def _error_result(block: ToolUseBlock, detail: str) -> tuple[ToolResultBlock, ToolCallRecord]:
    """Rule 4: a failed tool still gets a result block, carrying its tool_use_id."""
    return (
        ToolResultBlock(tool_use_id=block.id, content=detail, is_error=True),
        ToolCallRecord(tool_use_id=block.id, name=block.name, ok=False, detail=detail),
    )


def _answer_from(content: Sequence[ContentBlock]) -> str:
    """
    Join every text block into the final answer.

    Every text block, not the first: a turn can contain several, and with citations
    enabled it routinely does — the response splits at each cited span. Taking only the
    first would silently drop most of a cited answer. An empty result is legitimate, not
    an error.
    """
    return "".join(block.text for block in content if isinstance(block, TextBlock))
