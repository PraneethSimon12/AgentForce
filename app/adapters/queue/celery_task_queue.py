"""
The `TaskQueue` port, over Celery.

Two details carry the weight of this file.

**It dispatches by task name, not by importing the task.** `send_task` means the API
process never imports `app.workers.tasks`, and therefore never imports the tool handlers,
their dependencies, or Celery's task registry. The two processes share a string and a
ledger row, which is as loosely as they can be coupled while still agreeing on anything.

**Publishing is offloaded to a thread.** `send_task` opens a socket and writes to Redis;
it is ordinary blocking IO, and calling it from the event loop would stall every other
request on the worker for as long as the broker takes to answer. That is precisely the
class of bug the `ASYNC` ruff ruleset exists to catch (CLAUDE.md §4), and the fix is one
line as long as you notice you need it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from celery import Celery
from kombu.exceptions import OperationalError

from app.core.runtime.errors import TaskQueueUnavailable
from app.workers.celery_app import TOOL_TASK

if TYPE_CHECKING:
    from app.core.ports import TaskQueue


class CeleryTaskQueue:
    """Structurally a `TaskQueue`. Publishes; never consumes."""

    def __init__(self, app: Celery, *, task_name: str = TOOL_TASK) -> None:
        self._app = app
        self._task_name = task_name

    async def enqueue_tool(
        self,
        run_id: uuid.UUID,
        step_idx: int,
        tool_name: str,
        args: Mapping[str, Any],
        key: str,
    ) -> None:
        """
        Publish one tool invocation, off the event loop.

        Postcondition on return: the broker has accepted the message. It says nothing
        about the tool having started, and the caller must not infer otherwise — the only
        source of truth for that is the ledger.
        """
        await asyncio.to_thread(self._publish, run_id, step_idx, tool_name, args, key)

    def _publish(
        self,
        run_id: uuid.UUID,
        step_idx: int,
        tool_name: str,
        args: Mapping[str, Any],
        key: str,
    ) -> None:
        """
        The blocking half. Runs in a thread.

        `str(run_id)` because the payload is serialised as JSON and a UUID is not a JSON
        type. Converting here rather than relying on a serialiser's convenience keeps the
        wire form something both sides can state.
        """
        try:
            self._app.send_task(
                self._task_name,
                kwargs={
                    "run_id": str(run_id),
                    "step_idx": step_idx,
                    "tool_name": tool_name,
                    "args": dict(args),
                    "key": key,
                },
            )
        except OperationalError as exc:
            # kombu's wrapper for "could not reach or talk to the broker", after its own
            # publish retries are exhausted. Translated at the boundary so the loop can
            # apply its retry policy without importing kombu — and narrow rather than
            # broad, because a serialisation error here is a bug we want to see, not a
            # transport blip we want to retry (CLAUDE.md §4).
            raise TaskQueueUnavailable(f"Could not queue {tool_name!r}: {exc}") from exc


if TYPE_CHECKING:
    # Compile-time proof that the adapter satisfies the port. If a signature drifts, mypy
    # fails here rather than at the one call site that happens to exercise it.
    def _conforms(app: Celery) -> TaskQueue:
        return CeleryTaskQueue(app)
