"""
Running one tool call through the ledger. The protocol, in one place, for both processes.

v1.7 gave this project a second process that executes tools (D-023): the loop runs the
INLINE ones, a Celery worker runs the DURABLE ones. Both must follow the same sequence —
claim the invocation, execute only if the claim says to, record the outcome before doing
anything else — because that sequence *is* the exactly-once guarantee. Written twice it
would be two guarantees, and the day they drifted the symptom would be a tool that ran
twice in production and once in every test.

So it is written once, here, with no knowledge of who is calling. What this function
deliberately does **not** decide:

- **What a crash means.** It records the crash and re-raises. The loop turns that into a
  failed run because a bug should not be papered over; the worker lets it propagate so
  Celery logs the traceback, and the model still gets a failed `tool_result` because the
  row was recorded first. Same recording, different policy, and the policy belongs to
  the caller.
- **What the model is shown.** It returns an outcome, not a content block. Blocks are the
  loop's vocabulary.
- **Whether to dispatch.** By the time this runs, that decision is made.

`core/`, so no IO: the ledger arrives as a port and the registry is a plain object.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from app.core.runtime.errors import ToolCrashed, ToolExecutionFailed
from app.core.runtime.idempotency import (
    InvocationAction,
    InvocationOutcome,
    InvocationStatus,
)
from app.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from app.core.ports import ToolLedger

# A tool that returns half a megabyte would blow both the context window and the token
# budget, and it would do so on a path with no other limit on it — the budget is checked
# before a step, and this arrives during one. Truncating is the only bound available that
# does not require trusting every tool author. Applied before the result is recorded, so
# the cap also bounds what a durable tool can write into the ledger.
MAX_TOOL_RESULT_CHARS = 8_000


async def execute_invocation(
    *,
    registry: ToolRegistry,
    ledger: ToolLedger,
    run_id: uuid.UUID,
    step_idx: int,
    tool_name: str,
    args: Mapping[str, Any],
    key: str,
) -> InvocationOutcome:
    """
    Execute one tool call at most once, and return what the ledger now says about it.

    Responsibility: the claim/execute/record sequence. Does NOT choose the tool, does NOT
    decide what a failure means for the run, and does NOT format anything for the model.

    Preconditions: `key` was computed by `invocation_key` from these same four
    components. A key computed from anything else deduplicates nothing, silently.

    Postcondition: the ledger row for `key` is terminal, and the tool's handler was
    invoked either zero or one times **by this call**. Zero when the ledger already had
    an answer — which is the entire mechanism, and the reason this returns an outcome
    rather than a result: the caller cannot tell whether execution happened, and must
    not need to.

    Raises:
        ToolNotFound: the name is not in this process's registry. Not caught here,
            because in the loop it means the model invented a tool and in a worker it
            means the worker is running different code from the API — two very different
            problems that the two callers report differently.
        ToolInputInvalid: the arguments do not validate.
        ToolCrashed: the handler raised something that was not a declared failure. The
            outcome is recorded as an error *before* this is raised, so the invocation is
            never left PENDING by a bug — a PENDING row is a promise that someone is
            still working, and a crashed handler makes that a lie.
    """
    spec = registry.get(tool_name)
    payload = spec.validate_input(args)

    decision = await ledger.begin(run_id, step_idx, tool_name, spec.effect_class, key)

    match decision.action:
        case InvocationAction.REPLAY:
            # Already done, by an earlier attempt or a duplicate delivery. This is the
            # line that makes "executed exactly once" true across a crash.
            return InvocationOutcome(
                status=InvocationStatus.FAILED if decision.is_error else InvocationStatus.SUCCEEDED,
                result=decision.result,
                is_error=decision.is_error,
            )
        case InvocationAction.NEEDS_REVIEW:
            # An UNSAFE call whose first attempt vanished mid-flight. Recorded as a
            # terminal status rather than simply refused, because the caller may be a
            # worker whose only channel back to the waiting run is this row (D-023).
            reason = (
                f"Tool {tool_name!r} is UNSAFE and a previous attempt was interrupted "
                f"mid-flight. Replaying it could double its effect."
            )
            await ledger.needs_review(key, reason)
            return InvocationOutcome(
                status=InvocationStatus.NEEDS_REVIEW, result=reason, is_error=True
            )
        case InvocationAction.EXECUTE | InvocationAction.RE_EXECUTE:
            pass

    try:
        output = await spec.handler(payload)
    except ToolExecutionFailed as exc:
        # The tool declared that this call could not succeed. Recorded as the outcome,
        # not as a crash: the model reads the reason and tries something else, which
        # turns a dead run into one more step.
        await ledger.complete(key, str(exc), is_error=True)
        return InvocationOutcome(status=InvocationStatus.FAILED, result=str(exc), is_error=True)
    except Exception as exc:  # noqa: BLE001 — a third party's handler; recorded, then re-raised
        detail = f"A tool raised {type(exc).__name__}: {exc}"
        await ledger.complete(key, detail, is_error=True)
        raise ToolCrashed(tool_name, exc) from exc

    if len(output) > MAX_TOOL_RESULT_CHARS:
        output = (
            output[:MAX_TOOL_RESULT_CHARS]
            + f"\n[truncated: output exceeded {MAX_TOOL_RESULT_CHARS} characters]"
        )

    # Completed before the step is committed, which closes the window between "the tool
    # finished" and "the step was recorded" (D-021).
    await ledger.complete(key, output, is_error=False)
    return InvocationOutcome(status=InvocationStatus.SUCCEEDED, result=output, is_error=False)
