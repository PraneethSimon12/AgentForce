"""
The exception types `core/` raises and catches.

Only the ones something already raises or handles live here. The point of this module is
one distinction, made once: **retryable versus not.** CLAUDE.md §4 bans catching a single
broad exception class from the SDK precisely because that distinction is the entire
reason for catching at all — a 429 should back off and try again, a 400 never will
succeed and retrying it just spends money slowly.

The adapter is what maps provider exceptions onto these. That is what lets the loop
implement a retry policy (D-009) without importing `anthropic` and without knowing that
`RateLimitError` exists.
"""

from __future__ import annotations


class AgentForgeError(Exception):
    """Base for every error this system raises deliberately."""


class LLMError(AgentForgeError):
    """A call to the model provider failed."""


class LLMTransportError(LLMError):
    """
    A provider failure that may succeed if tried again.

    Connection failures, timeouts, 429s, 5xx. The loop's bounded retry applies to these
    and only these.
    """


class LLMRequestError(LLMError):
    """
    A provider failure that will never succeed if tried again.

    A malformed request, an unknown model, a bad API key, a request over the context
    limit. Retrying is not just useless, it is expensive and it hides the real bug, so
    the loop fails the run and records why.
    """


class ToolError(AgentForgeError):
    """Something is wrong with a tool definition, or with a call to one."""


class InvalidToolSpec(ToolError):
    """
    A tool definition the API would reject.

    Raised while the tool is being defined, which means at import time — the process
    refuses to start rather than failing on the first call that happens to use it.
    """


class DuplicateToolName(ToolError):
    """Two tools registered under one name. The second registration is refused."""


class ToolNotFound(ToolError):
    """
    The model named a tool that is not in the registry.

    Carries the available names so the error message is actionable, and so the loop can
    hand the model a `tool_result` that tells it what it *could* have called.
    """

    def __init__(self, name: str, available: tuple[str, ...]) -> None:
        super().__init__(f"No tool named {name!r}. Registered: {', '.join(available) or '(none)'}")
        self.name = name
        self.available = available


class ToolInputInvalid(ToolError):
    """
    The model's arguments failed the tool's input model.

    `detail` is kept as a separate, compact field rather than only inside the message,
    because it goes back to the model in a `tool_result` with `is_error=True`. The model
    then corrects its own call, which turns a validation failure into a recoverable step
    instead of a failed run — but only if the detail says which field was wrong and why.
    """

    def __init__(self, tool_name: str, detail: str) -> None:
        super().__init__(f"Invalid arguments for tool {tool_name!r}: {detail}")
        self.tool_name = tool_name
        self.detail = detail


class ToolExecutionFailed(ToolError):
    """
    The tool ran and failed for a reason the model should be told about.

    Deliberately distinct from an unexpected exception escaping a handler. This one says
    "the call was well-formed but could not succeed" — division by zero, a document that
    does not exist — and the loop turns it into a tool_result with is_error=True so the
    model can try something else. An exception that is *not* this type is a bug in the
    tool, and the loop must not paper over it by feeding it back as ordinary output.
    """


class StoreError(AgentForgeError):
    """Something went wrong reaching or reading durable run state."""


class RunNotFound(StoreError):
    """No run with that id. A 404 at the edge (plan.md §2.1)."""


class StepAlreadyCommitted(StoreError):
    """
    A step index that is already present in the store.

    Raised by the UNIQUE(run_id, idx) constraint, and it is less an error than an
    answer: after a crash, "this step is already committed" is exactly what a resuming
    worker needs to be told, and it is the reason the constraint exists.
    """


class RunNotResumable(StoreError):
    """The run is terminal, or its lease is held and still live. A 409 at the edge."""


class StepTimeout(AgentForgeError):
    """
    A single step exceeded its time budget.

    Retryable, because the usual cause is a slow upstream rather than a wrong request.
    Distinct from the *run* having no time left: this bounds one model call plus its
    tools, which is what stops a single wedged step from holding a lease forever while
    the worker that owns it waits politely.
    """

    def __init__(self, step_idx: int, seconds: float) -> None:
        super().__init__(f"Step {step_idx} exceeded its budget of {seconds}s.")
        self.step_idx = step_idx
        self.seconds = seconds


class RetriesExhausted(AgentForgeError):
    """
    The step failed more times than its budget allows, or the run ran out of retries.

    Two bounds, both reachable (D-009). The per-step one catches a step that cannot
    succeed; the per-run one catches a run that keeps almost-succeeding and would
    otherwise retry forever at a slow burn.
    """

    def __init__(
        self, message: str, *, last_error: str, run_budget_exhausted: bool = False
    ) -> None:
        super().__init__(message)
        self.last_error = last_error
        self.run_budget_exhausted = run_budget_exhausted
        """Which bound was hit. The step bound pauses the run; the run bound ends it."""


class QueueError(AgentForgeError):
    """Something went wrong handing work to another process."""


class TaskQueueUnavailable(QueueError):
    """
    The broker could not be reached, so a durable tool was never queued.

    Retryable, and retried by the same per-step bound that covers a provider blip: the
    tool has definitely not run, so trying again risks nothing. The alternative — failing
    the run because Redis was restarting — would make the queue a single point of failure
    for work that has not even started.
    """


class QueueNotConfigured(QueueError):
    """
    A DURABLE tool is registered but there is no queue to dispatch it to.

    Raised while the loop is being constructed, not when the model first asks for the
    tool. The difference is a process that refuses to start versus a run that dies
    halfway through at 3am — and the registry is fixed at construction, so the check is
    answerable there.
    """


class ToolCrashed(ToolError):
    """
    An unexpected exception escaped a tool handler. Not a declared failure — a bug.

    Distinct from `ToolExecutionFailed`, which means "the call was well-formed but could
    not succeed" and is fed back to the model so it can try something else. This one
    means the tool is broken, and the two callers treat it differently on purpose: the
    loop fails the run rather than papering over a bug, while a Celery worker lets it
    propagate so the traceback reaches the worker log. In both cases the ledger row was
    already completed before this was raised, so a bug never leaves an invocation
    PENDING — which would otherwise read as "still working" forever.
    """

    def __init__(self, tool_name: str, cause: Exception) -> None:
        super().__init__(f"Tool {tool_name!r} raised {type(cause).__name__}: {cause}")
        self.tool_name = tool_name
        self.cause = cause
