"""
What a run *is*, as data. No behaviour, no IO — just the shapes the loop produces.

These types are deliberately the shape v1 will persist. `StepRecord` is one row of the
`run_steps` table before that table exists, so the durability phase is adding storage to
a structure that already exists rather than reshaping the loop around a schema.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.core.runtime.messages import Effort, Message, StopReason, Usage


class RunStatus(StrEnum):
    """
    Where a run is in its lifecycle.

    Note what is *not* here: a reason. "Why did it fail" is a separate field, because
    folding it in produces an enum that grows every time a new failure mode appears and
    forces every client switching on status to handle values it has never heard of.

    The transitions are QUEUED -> RUNNING -> {COMPLETED, FAILED, PAUSED}, and
    PAUSED -> RUNNING on resume. A run is recorded before it starts (plan.md §2.2), so
    QUEUED is a real state and not a placeholder.
    """

    QUEUED = "QUEUED"
    """Recorded, not started. `POST /v1/runs` returns here in single-digit milliseconds."""

    RUNNING = "RUNNING"
    """Claimed by a worker holding a live lease."""

    PAUSED = "PAUSED"
    """Stopped mid-run and resumable: a cancel, or a lease that expired after a crash."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        """COMPLETED and FAILED are final; a resume against either is a 409."""
        return self in (RunStatus.COMPLETED, RunStatus.FAILED)


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

    NEEDS_REVIEW = "NEEDS_REVIEW"
    """
    An UNSAFE tool was interrupted mid-flight and a human must resolve it.

    Terminal by design, not by limitation (D-004): the alternatives are replaying a
    call that might double its effect, or discarding a run that may have already
    charged someone. Neither is ours to choose silently.
    """


@dataclass(frozen=True, slots=True)
class RunLimits:
    """
    The ceilings for one run. Defaults mirror `.env.example`; settings clamp them.

    A client may ask for *less* than the configured ceiling but never more (plan.md
    §2.2) — otherwise the budget is advisory, which is the same as absent.

    Resolved once, at run creation, and then stored on the run row. A resumed run must
    use the limits it started with: re-reading them from settings would mean a config
    change silently moved the cost ceiling of a run already in flight, and the audit
    trail would not show it.
    """

    max_steps: int = 12
    token_budget: int = 120_000
    max_output_tokens: int = 16_000


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


@dataclass(frozen=True, slots=True)
class NewRun:
    """
    Everything needed to record a run before it starts.

    `prompt_name`/`prompt_version` and `model` are captured here rather than looked up
    at execution time, because they are what the result has to be attributable to
    (D-011). A run whose prompt version is unknown cannot be used as an eval data point.
    """

    agent: str
    input: dict[str, Any]
    limits: RunLimits
    effort: Effort
    prompt_name: str
    prompt_version: int
    model: str
    tenant_id: str = "default"
    parent_run_id: uuid.UUID | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class RunRecord:
    """
    A run as it currently stands in the store, including enough to resume it.

    `messages` is the replay payload. It comes back as blocks rebuilt from JSONB by
    `block_from_dict`, which is the same function the provider adapter uses — one
    implementation, so a resumed conversation cannot differ from the original.
    """

    id: uuid.UUID
    agent: str
    status: RunStatus
    input: dict[str, Any]
    limits: RunLimits
    effort: Effort
    prompt_name: str
    prompt_version: int
    model: str
    usage: RunUsage
    messages: tuple[Message, ...]
    steps: tuple[StepRecord, ...]
    tenant_id: str = "default"
    parent_run_id: uuid.UUID | None = None
    answer: str | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None

    @property
    def next_step_idx(self) -> int:
        """
        The index the next step must claim.

        Derived from committed steps rather than stored as a counter, because a counter
        and the rows it counts are two facts that can disagree after a crash. The rows
        are the truth; `UNIQUE(run_id, idx)` enforces it.
        """
        return len(self.steps)
