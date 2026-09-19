"""
The ReAct loop: model → tool → result → model, until an answer or a limit.

Hand-written rather than the SDK's `tool_runner` (D-002), because the loop is where
every requirement of this project lives: a durable checkpoint between steps, a per-step
timeout, tool calls routed to Celery, and resumption from Postgres on another machine.
None of those are hooks the runner exposes.

v0 runs entirely in memory. What is here now is the control flow and the protocol
handling; v1 adds a commit between every step without changing the shape, which is why
`RunOutcome` and `StepRecord` already look like rows.

The four protocol rules this file exists to get right (CLAUDE.md §8):

1. **`stop_reason` is read before `content`, every time.** A refusal is an HTTP 200 with
   an empty content list, and a `max_tokens` truncation is a valid-looking response with
   a broken intent. Both are indistinguishable from success if you read the blocks first.
2. **The assistant turn is appended verbatim.** Raw blocks, including opaque thinking
   blocks, so they round-trip byte-identically (D-014).
3. **All tool results from one turn go back in a single user message.** Splitting them
   silently teaches the model to stop calling tools in parallel. Nothing errors.
4. **Every tool_use gets a result, including a failed one**, carrying its `tool_use_id`
   and `is_error=True`. A dropped result leaves the model with an unanswered question.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.ports import LLMClient
from app.core.runtime.errors import (
    ToolExecutionFailed,
    ToolInputInvalid,
    ToolNotFound,
)
from app.core.runtime.messages import (
    ContentBlock,
    Effort,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from app.core.runtime.policy import BudgetExceeded, LoopPolicy
from app.core.runtime.state import (
    ErrorCode,
    RunOutcome,
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


class AgentLoop:
    """
    Drives one run to a terminal state.

    Responsibility: control flow and the block protocol. Does NOT choose tools (the
    model does), does NOT decide the limits (the policy does), does NOT talk to a
    provider (the client does), and does NOT persist anything (v1's store will).

    Holds no run state of its own — everything lives in local variables inside `run()`.
    That is what makes one instance safe to share across concurrent runs, and it is the
    shape v1 needs anyway, when the state has to come from a database instead.
    """

    def __init__(
        self,
        *,
        llm: LLMClient,
        registry: ToolRegistry,
        policy: LoopPolicy,
        system_prompt: str,
        effort: Effort = "medium",
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._policy = policy
        self._system_prompt = system_prompt
        self._effort = effort

    async def run(self, user_input: str) -> RunOutcome:
        """
        Execute a run from a user message to a terminal outcome.

        Postcondition: the returned outcome is terminal. The loop never returns while it
        still intends to continue, and it cannot loop forever — every iteration either
        returns or is authorised by the policy, which counts steps monotonically.

        Raises: nothing under normal operation. A bug inside a tool becomes a FAILED
        outcome with TOOL_FAILED rather than an exception, because a worker that dies on
        a bad tool takes every other run on that process with it.
        """
        messages: list[Message] = [Message(role="user", content=[TextBlock(text=user_input)])]
        steps: list[StepRecord] = []
        usage = RunUsage()

        while True:
            try:
                self._policy.check(usage)
            except BudgetExceeded as exc:
                return self._failed(ErrorCode.BUDGET_EXCEEDED, str(exc), usage, messages, steps)

            response = await self._llm.complete(
                messages=messages,
                system=self._system_prompt,
                tools=self._registry.schemas(),
                max_tokens=self._policy.max_tokens_for_next_step(usage),
                effort=self._effort,
            )

            step_idx = usage.steps
            usage = usage.plus(response.usage)

            # Rule 1: stop_reason first. Everything below this line may read content.
            if response.stop_reason == "refusal":
                steps.append(StepRecord(step_idx, response.stop_reason, response.usage))
                category = response.stop_details.category if response.stop_details else None
                return self._failed(
                    ErrorCode.UPSTREAM_REFUSAL,
                    f"The model declined to continue (category: {category}).",
                    usage,
                    messages,
                    steps,
                )

            if response.stop_reason in ("max_tokens", "pause_turn"):
                steps.append(StepRecord(step_idx, response.stop_reason, response.usage))
                return self._failed(
                    ErrorCode.UPSTREAM_TRUNCATED,
                    f"The model stopped with {response.stop_reason!r}; its output is "
                    f"incomplete and may contain a partial tool call.",
                    usage,
                    messages,
                    steps,
                )

            # Rule 2: verbatim, opaque blocks and all.
            messages.append(Message(role="assistant", content=list(response.content)))

            if response.stop_reason == "tool_use":
                requested = [b for b in response.content if isinstance(b, ToolUseBlock)]
                if not requested:
                    # The provider said "tool_use" and sent no tool_use block. Appending
                    # an empty user message here would produce a malformed request, so
                    # the run stops instead of constructing one.
                    steps.append(StepRecord(step_idx, response.stop_reason, response.usage))
                    return self._failed(
                        ErrorCode.UPSTREAM_TRUNCATED,
                        "The model signalled a tool call but emitted no tool_use block.",
                        usage,
                        messages,
                        steps,
                    )

                try:
                    results, records = await self._execute_all(requested)
                except Exception as exc:  # noqa: BLE001 — see below
                    # Deliberately broad, and the only place in the codebase that is.
                    # A tool raising something other than ToolExecutionFailed is a bug
                    # in that tool, and the correct response to a bug at a worker
                    # boundary is to record the run as failed — not to let one bad tool
                    # kill the process and every other run sharing it. The exception
                    # type is preserved in the message; the traceback goes to the log.
                    steps.append(StepRecord(step_idx, response.stop_reason, response.usage))
                    return self._failed(
                        ErrorCode.TOOL_FAILED,
                        f"A tool raised {type(exc).__name__}: {exc}",
                        usage,
                        messages,
                        steps,
                    )

                steps.append(
                    StepRecord(step_idx, response.stop_reason, response.usage, tuple(records))
                )
                # Rule 3: one user message, every result inside it.
                messages.append(Message(role="user", content=list(results)))
                continue

            # end_turn or stop_sequence: the model is done.
            steps.append(StepRecord(step_idx, response.stop_reason, response.usage))
            return RunOutcome(
                status=RunStatus.COMPLETED,
                usage=usage,
                messages=tuple(messages),
                steps=tuple(steps),
                answer=_answer_from(response.content),
            )

    async def _execute_all(
        self, requested: Sequence[ToolUseBlock]
    ) -> tuple[list[ToolResultBlock], list[ToolCallRecord]]:
        """
        Run every tool the model asked for, in the order it asked.

        Sequential, not concurrent, and that is a v0 decision rather than an oversight:
        both current tools complete in microseconds, so `gather` would buy nothing
        measurable while complicating error attribution. The protocol-level support for
        parallel calls — several tool_use blocks in one turn, all results in one user
        message — is here and tested; only the execution is serial. It becomes `gather`
        when a tool is slow enough for it to show up in a measurement.
        """
        results: list[ToolResultBlock] = []
        records: list[ToolCallRecord] = []
        for block in requested:
            result, record = await self._execute_one(block)
            results.append(result)
            records.append(record)
        return results, records

    async def _execute_one(self, block: ToolUseBlock) -> tuple[ToolResultBlock, ToolCallRecord]:
        """
        Run one tool call, converting a *declared* failure into a result the model can use.

        Three failures are recoverable and get handed back with `is_error=True`: the tool
        does not exist, the arguments do not validate, or the tool declared that it could
        not succeed. In all three the model can read the reason and try something else,
        which turns a dead run into one more step.

        Anything else propagates. That is the line between "the tool failed" and "the
        tool is broken", and blurring it would mean shipping a bug to production
        disguised as a tool result the model politely worked around.

        Raises: whatever an unexpected tool bug raises.
        """
        try:
            spec = self._registry.get(block.name)
            payload = spec.validate_input(block.input)
            output = await spec.handler(payload)
        except ToolInputInvalid as exc:
            return self._error_result(block, exc.detail)
        except (ToolNotFound, ToolExecutionFailed) as exc:
            return self._error_result(block, str(exc))

        if len(output) > MAX_TOOL_RESULT_CHARS:
            output = (
                output[:MAX_TOOL_RESULT_CHARS]
                + f"\n[truncated: output exceeded {MAX_TOOL_RESULT_CHARS} characters]"
            )
        return (
            ToolResultBlock(tool_use_id=block.id, content=output),
            ToolCallRecord(tool_use_id=block.id, name=block.name, ok=True),
        )

    @staticmethod
    def _error_result(block: ToolUseBlock, detail: str) -> tuple[ToolResultBlock, ToolCallRecord]:
        """Rule 4: a failed tool still gets a result block, carrying its tool_use_id."""
        return (
            ToolResultBlock(tool_use_id=block.id, content=detail, is_error=True),
            ToolCallRecord(tool_use_id=block.id, name=block.name, ok=False, detail=detail),
        )

    @staticmethod
    def _failed(
        code: ErrorCode,
        message: str,
        usage: RunUsage,
        messages: Sequence[Message],
        steps: Sequence[StepRecord],
    ) -> RunOutcome:
        """Build a terminal failure that still carries the full conversation and trail."""
        return RunOutcome(
            status=RunStatus.FAILED,
            usage=usage,
            messages=tuple(messages),
            steps=tuple(steps),
            error_code=code,
            error_message=message,
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
