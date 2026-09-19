"""
What a run *is*, as data. No behaviour, no IO — just the shapes the loop produces.

These types are deliberately the shape v1 will persist. `StepRecord` is one row of the
`run_steps` table before that table exists, so the durability phase is adding storage to
a structure that already exists rather than reshaping the loop around a schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.core.runtime.messages import Message, StopReason, Usage


class RunStatus(StrEnum):
    """
    Terminal state of a run.

    Only two, because "why did it fail" is a separate field. Folding the reason into the
    status produces an enum that grows every time a new failure mode appears, and forces
    every client switching on status to handle values it has never heard of.
    """

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ErrorCode(StrEnum):
    """
    Stable, machine-readable failure reasons. Clients switch on these; they never change.

    The wire contract (plan.md §2.1) holds the full table including the HTTP-level codes.
    These are the subset the *loop* can produce — a run that was created successfully and
    then failed while executing.
    """

    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    """The step cap or the token budget was reached. A run outcome, not an HTTP error."""

    UPSTREAM_REFUSAL = "UPSTREAM_REFUSAL"
    """The model declined. Arrives as an HTTP 200 with stop_reason "refusal"."""

    UPSTREAM_TRUNCATED = "UPSTREAM_TRUNCATED"
    """
    The model was cut off at max_tokens.

    Terminal rather than retryable, because the dangerous case is being truncated
    *mid-tool-call*: the response is syntactically valid and its intent is broken, so
    continuing would act on half an instruction (CLAUDE.md §8).
    """

    TOOL_FAILED = "TOOL_FAILED"
    """A tool raised something that was not a declared, recoverable failure — a bug."""


@dataclass(frozen=True, slots=True)
class RunUsage:
    """Everything the budget is measured against. Accumulated across steps."""

    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Input plus output. Cached reads are counted in `input_tokens` by the API."""
        return self.input_tokens + self.output_tokens

    def plus(self, usage: Usage) -> RunUsage:
        """Return a new total including one more step. Frozen, so never mutated in place."""
        return RunUsage(
            steps=self.steps + 1,
            input_tokens=self.input_tokens + usage.input_tokens,
            output_tokens=self.output_tokens + usage.output_tokens,
            cache_read_tokens=self.cache_read_tokens + usage.cache_read_input_tokens,
        )


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One tool invocation inside a step. `ok=False` means the model was told it failed."""

    tool_use_id: str
    name: str
    ok: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class StepRecord:
    """
    One completed step: one model call plus whatever tools it asked for.

    This is the audit trail. In v1 it becomes a committed row, and the fact that it is
    already a flat, serialisable record is what makes that a storage change rather than
    a redesign.
    """

    idx: int
    stop_reason: StopReason
    usage: Usage
    tool_calls: tuple[ToolCallRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """
    The result of a run, successful or not.

    `messages` is the full conversation as it would be replayed — including the raw
    assistant blocks, verbatim. It is here rather than reconstructed from `steps`
    because v1 replays *this* to resume, and anything reconstructed is a chance to get
    the round trip wrong (D-014).
    """

    status: RunStatus
    usage: RunUsage
    messages: tuple[Message, ...]
    steps: tuple[StepRecord, ...] = field(default=())
    answer: str | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
