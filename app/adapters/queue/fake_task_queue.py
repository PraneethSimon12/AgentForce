"""
A `TaskQueue` that stands in for Celery, and for the worker on the other end of it.

Scripted, not random — the same rule `FakeLLM` follows. A test says exactly when the
"worker" runs the tool, so the loop's dispatch, its waiting, its resumption and its
in-flight pause are all deterministically reachable without a broker, a second process
or a sleep.

What makes this a fake rather than a mock: it executes the tool through
`execute_invocation`, the same function the real Celery task calls. So a test that
asserts "the tool ran exactly once across a duplicate delivery" is asserting it about
production code, and the only thing being faked is the transport.

The one behaviour worth calling out is that `ToolCrashed` is swallowed. That is not
laziness — it is the process boundary being reproduced faithfully. A tool that blows up
in a Celery worker raises in *that* process; the exception cannot cross to the loop, and
all the loop ever sees is the ledger row the worker wrote before it died. A fake that
let the exception through would make the loop look robust to something it never faces.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import Any

from app.core.ports import ToolLedger
from app.core.runtime.errors import TaskQueueUnavailable, ToolCrashed, ToolError
from app.core.tools.execution import execute_invocation
from app.core.tools.registry import ToolRegistry


class Delivery(StrEnum):
    """When the fake worker picks the task up."""

    IMMEDIATE = auto()
    """Executes during `enqueue_tool`, so the loop's first poll already finds a result."""

    DEFERRED = auto()
    """Queued and not executed until the test calls `deliver_all`. A slow worker — or,
    if the test never calls it, one that died before starting."""

    UNAVAILABLE = auto()
    """The broker is unreachable. `enqueue_tool` raises and nothing is recorded."""


@dataclass(frozen=True, slots=True)
class Dispatch:
    """One enqueued invocation, as the worker would receive it."""

    run_id: uuid.UUID
    step_idx: int
    tool_name: str
    args: Mapping[str, Any]
    key: str


@dataclass
class FakeTaskQueue:
    """Structurally a `TaskQueue`, with a worker attached."""

    registry: ToolRegistry
    ledger: ToolLedger
    mode: Delivery = Delivery.IMMEDIATE

    dispatched: list[Dispatch] = field(default_factory=list)
    """Every accepted enqueue, in order. Duplicates are visible here, which is the point
    — the loop must not produce them."""

    undelivered: list[Dispatch] = field(default_factory=list)
    """Accepted but not yet executed. `deliver_all` drains this."""

    crashes: list[ToolCrashed] = field(default_factory=list)
    """Exceptions that died inside the fake worker, kept so a test can assert on what
    the real one would only have written to a log."""

    async def enqueue_tool(
        self,
        run_id: uuid.UUID,
        step_idx: int,
        tool_name: str,
        args: Mapping[str, Any],
        key: str,
    ) -> None:
        if self.mode is Delivery.UNAVAILABLE:
            # Raised before anything is recorded, matching the real adapter: a publish
            # that failed means the task does not exist, so the caller may safely retry.
            raise TaskQueueUnavailable("Fake broker is unavailable.")

        dispatch = Dispatch(run_id, step_idx, tool_name, dict(args), key)
        self.dispatched.append(dispatch)

        if self.mode is Delivery.IMMEDIATE:
            await self._run(dispatch)
        else:
            self.undelivered.append(dispatch)

    async def deliver_all(self) -> None:
        """Run every queued invocation, as a worker coming back to a full queue would."""
        pending, self.undelivered = self.undelivered, []
        for dispatch in pending:
            await self._run(dispatch)

    async def redeliver(self, index: int = -1) -> None:
        """
        Deliver an already-delivered task a second time. At-least-once, on demand.

        This is the whole reason `acks_late` needs the ledger: a worker that dies after
        running a tool gets the task again, and nothing except the recorded row stops the
        side effect happening twice.
        """
        await self._run(self.dispatched[index])

    async def _run(self, dispatch: Dispatch) -> None:
        try:
            await execute_invocation(
                registry=self.registry,
                ledger=self.ledger,
                run_id=dispatch.run_id,
                step_idx=dispatch.step_idx,
                tool_name=dispatch.tool_name,
                args=dispatch.args,
                key=dispatch.key,
            )
        except ToolCrashed as exc:
            # Died in the worker. The ledger row was completed before this was raised,
            # so the loop still gets an answer; the traceback simply does not cross.
            self.crashes.append(exc)
        except ToolError as exc:
            # A worker whose registry disagrees with the API's, or arguments that did not
            # validate there. No ledger row exists, so the loop will wait until its step
            # budget runs out — which is the real behaviour, and worth being able to test.
            self.crashes.append(ToolCrashed(dispatch.tool_name, exc))
