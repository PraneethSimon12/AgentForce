"""
The durable-tool task. Deliberately thin.

Everything that decides anything lives in `core/tools/execution.py`, which the loop calls
too (D-023). What is left here is the part that can only exist in a worker: unpack a JSON
payload, get this process's resources, run the coroutine, and let failures reach Celery's
log. That thinness is the point — the exactly-once logic is unit-tested with no broker,
because there is nothing in it that needs one.

**The task body must be safe to run twice.** Not "unlikely to", *safe to*. Late
acknowledgement plus `task_reject_on_worker_lost` means a task whose worker is SIGKILLed
comes back, and Redis will redeliver anything that outruns the visibility timeout. Both
are handled the same way and in only one place: the ledger row, claimed before execution.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from app.core.runtime.errors import ToolCrashed, ToolInputInvalid, ToolNotFound
from app.core.tools.execution import execute_invocation
from app.workers.celery_app import TOOL_TASK, celery_app
from app.workers.runtime import resources, run_async

log = structlog.get_logger(__name__)


# Celery's task decorator is untyped, so mypy --strict would call everything it
# wraps untyped too. Ignored here alone; the function's own signature is checked.
@celery_app.task(name=TOOL_TASK, bind=False)  # type: ignore[untyped-decorator]
def execute_tool(
    *,
    run_id: str,
    step_idx: int,
    tool_name: str,
    args: dict[str, Any],
    key: str,
) -> str:
    """
    Execute one durable tool call and record the outcome in the ledger.

    Returns the terminal ledger status as a string, for the worker log only. **Nothing
    reads the return value** — there is no result backend, and the run collects its answer
    by reading the row (D-023). Returning something is worth more in a log line than
    returning None.

    Keyword-only, and JSON-serialisable throughout, because these arguments cross a
    process boundary as JSON. `run_id` arrives as a string for the same reason: a UUID is
    not JSON, and letting the serialiser guess is how you get a string on one side and a
    UUID on the other, hashing to two different idempotency keys.

    Raises: ToolCrashed, after the outcome has been recorded, so the traceback reaches the
        worker log instead of vanishing. Celery treats that as a task failure and does
        **not** redeliver — correctly, because a tool that raises on this input will raise
        on it again, and the ledger already holds the error the run needs.
    """
    resource = resources()
    try:
        status = run_async(
            execute_invocation(
                registry=resource.registry,
                ledger=resource.ledger,
                run_id=uuid.UUID(run_id),
                step_idx=step_idx,
                tool_name=tool_name,
                args=args,
                key=key,
            )
        )
    except (ToolNotFound, ToolInputInvalid):
        # The API validated both of these before dispatching, so reaching here means the
        # worker is running different code from the API that enqueued the task — a bad
        # deploy, not a bad request. Logged loudly and left alone: no ledger row is
        # written, so the run waits out its step budget and pauses rather than being
        # handed an error that would send the model off correcting a call that was fine.
        log.exception("durable_tool.unroutable", tool=tool_name, run_id=run_id, step_idx=step_idx)
        raise
    except ToolCrashed:
        log.exception("durable_tool.crashed", tool=tool_name, run_id=run_id, step_idx=step_idx)
        raise

    # No arguments and no result: tool arguments can carry user data and results can be
    # long, and both are already stored in the ledger row this line is about
    # (CLAUDE.md §4). The key is enough to find it.
    log.info(
        "durable_tool.completed",
        tool=tool_name,
        run_id=run_id,
        step_idx=step_idx,
        status=status.status.value,
        key=key,
    )
    return str(status.status.value)
