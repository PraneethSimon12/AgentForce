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
   (D-021).
7. **The lease is renewed at every step boundary.** A worker that has lost its lease
   stops there rather than writing alongside its replacement.
8. **Retries are bounded twice and counted deliberately** (D-009): per step in memory,
   per run on the row, with the SDK's own two retries underneath. Knowing there are
   three layers is the point — multiply them without noticing and one user action
   becomes eighteen attempts.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.core.ports import Clock, LLMClient, RunStore, ToolLedger
from app.core.runtime.errors import (
    LLMTransportError,
    RetriesExhausted,
    RunNotResumable,
    StepAlreadyCommitted,
    StepTimeout,
    ToolExecutionFailed,
    ToolInputInvalid,
    ToolNotFound,
)
from app.core.runtime.idempotency import InvocationAction, invocation_key
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
from app.core.tools.registry import ToolRegistry

# A tool that returns half a megabyte would blow both the context window and the token
# budget, and it would do so on a path with no other limit on it — the budget is checked
# before a step, and this arrives during one. Truncating is the only bound available
# that does not require trusting every tool author.
MAX_TOOL_RESULT_CHARS = 8_000


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
        backoff: BackoffPolicy | None = None,
        rng: Callable[[], float] = random.random,
        lease_ttl_seconds: int = 60,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._store = store
        self._ledger = ledger
        self._clock = clock
        self._load_prompt = load_prompt
        self._backoff = backoff or BackoffPolicy()
        # Injected so the jitter is deterministic under test. The only nondeterminism in
        # the retry path, kept visible rather than reached for inside the policy.
        self._rng = rng
        self._lease_ttl = lease_ttl_seconds

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

        Retryable: transport failures and timeouts. Not retryable: `LLMRequestError` (a
        400 never becomes a 200, and retrying spends money slowly while hiding the bug),
        `_NeedsReview`, and unexpected tool exceptions.

        Raises: RetriesExhausted when either bound is reached; anything non-retryable,
            untouched.
        """
        attempt = 1
        while True:
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
                        results, records = await self._execute_all(run_id, step_idx, requested)
                return _Attempt(response, results, records, attempt)

            except TimeoutError as exc:
                failure: Exception = StepTimeout(step_idx, limits.step_timeout_seconds)
                failure.__cause__ = exc
            except LLMTransportError as exc:
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
        self, run_id: uuid.UUID, step_idx: int, requested: Sequence[ToolUseBlock]
    ) -> tuple[list[ToolResultBlock], list[ToolCallRecord]]:
        """
        Run every tool the model asked for, in the order it asked.

        Sequential, not concurrent, and that is a decision rather than an oversight: the
        current tools complete in microseconds, so `gather` would buy nothing measurable
        while complicating both error attribution and the ledger's ordering. The
        protocol-level support for parallel calls — several tool_use blocks in one turn,
        all results in one user message — is here and tested; only the execution is
        serial, and it becomes `gather` when a tool is slow enough to show up in a
        measurement.
        """
        results: list[ToolResultBlock] = []
        records: list[ToolCallRecord] = []
        for block in requested:
            result, record = await self._execute_one(run_id, step_idx, block)
            results.append(result)
            records.append(record)
        return results, records

    async def _execute_one(
        self, run_id: uuid.UUID, step_idx: int, block: ToolUseBlock
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

        Raises: _NeedsReview for an interrupted UNSAFE tool; whatever an unexpected tool
            bug raises.
        """
        try:
            spec = self._registry.get(block.name)
            payload = spec.validate_input(block.input)
        except ToolInputInvalid as exc:
            return _error_result(block, exc.detail)
        except ToolNotFound as exc:
            return _error_result(block, str(exc))

        key = invocation_key(run_id, step_idx, block.name, block.input)
        decision = await self._ledger.begin(run_id, step_idx, block.name, spec.effect_class, key)

        match decision.action:
            case InvocationAction.REPLAY:
                # The tool already ran to completion in a previous attempt. This is the
                # line that makes "executed exactly once" true across a crash.
                recorded = decision.result or ""
                if decision.is_error:
                    return _error_result(block, recorded)
                return _ok_result(block, recorded)
            case InvocationAction.NEEDS_REVIEW:
                raise _NeedsReview(block.name)
            case InvocationAction.EXECUTE | InvocationAction.RE_EXECUTE:
                pass

        try:
            output = await spec.handler(payload)
        except ToolExecutionFailed as exc:
            await self._ledger.complete(key, str(exc), is_error=True)
            return _error_result(block, str(exc))
        except Exception as exc:  # noqa: BLE001 — a third party's handler; see _ToolBug
            raise _ToolBug(exc) from exc

        if len(output) > MAX_TOOL_RESULT_CHARS:
            output = (
                output[:MAX_TOOL_RESULT_CHARS]
                + f"\n[truncated: output exceeded {MAX_TOOL_RESULT_CHARS} characters]"
            )
        # Completed before the step is committed, which closes the window between "the
        # tool finished" and "the step was recorded" (D-021).
        await self._ledger.complete(key, output, is_error=False)
        return _ok_result(block, output)


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
