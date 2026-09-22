"""
Making a tool that ran once not run twice.

The crash window cannot be closed. The sequence is: write the ledger row, run the tool,
record the result, commit the step. The tool's side effect lands outside our database,
so no transaction spans it, and a process killed between "the tool ran" and "we wrote
down that it ran" leaves a question nothing in our system can answer.

    t0  ledger row written PENDING          (committed)
    t1  tool executes                        <- side effect happens here, outside Postgres
    t2  ledger row completed with result    (committed)
    t3  step row committed                  (committed)

Crash between **t2 and t3**: the ledger says SUCCEEDED and holds the result. A resumed
run redoes the step, finds the recorded result, and returns it *without executing
again*. No ambiguity at all — this window is fully closed.

Crash between **t1 and t2**: the ledger still says PENDING and the tool may or may not
have run. This is the window that cannot be closed, only made safe, and the only party
who can say whether replaying is acceptable is the person who wrote the tool. That is
why `effect_class` is a required field with no default (D-004).

Exactly-once is not achievable. At-least-once delivery with effectively-once *outcomes*
is, and only for tools that can say they are safe to replay.

A `DURABLE` tool (D-023) runs this same sequence in a Celery worker instead of in the
loop's process, which moves t0-t2 out of the process that is about to be killed: the run
crashes, the tool finishes anyway, and the resumed run finds SUCCEEDED. The window is not
abolished — the Celery worker can die too, and at-least-once redelivery then re-runs the
task — which is exactly why the task executes this protocol rather than trusting the
queue. Same timeline, different process, and `effect_class` still answers the same
question at the end of it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.core.tools.base import EffectClass


class InvocationStatus(StrEnum):
    """Where a recorded tool invocation got to."""

    PENDING = "PENDING"
    """Written before execution. Found after a crash, it means "unknown"."""

    SUCCEEDED = "SUCCEEDED"
    """Completed, result recorded. A replay returns the result and does not re-execute."""

    FAILED = "FAILED"
    """Completed with a declared failure. Also replayed, not re-attempted."""

    NEEDS_REVIEW = "NEEDS_REVIEW"
    """
    Found PENDING by a second attempt at an UNSAFE tool. Nobody will run it again.

    Terminal, and written by whichever attempt discovered the ambiguity — which for a
    DURABLE tool is a redelivered Celery task, running in a process the loop cannot see.
    The status is how it tells the loop, because the only channel between those two
    processes is this row (D-023). Without it the run would wait for a result that is
    never coming, and report patience where it should report a decision.
    """


class InvocationAction(StrEnum):
    """What the loop should do about a tool call, having consulted the ledger."""

    EXECUTE = "EXECUTE"
    """No prior record. Run it."""

    REPLAY = "REPLAY"
    """A completed record exists. Return it; do not run the tool again."""

    RE_EXECUTE = "RE_EXECUTE"
    """A PENDING record exists and this tool declared replay to be safe."""

    NEEDS_REVIEW = "NEEDS_REVIEW"
    """
    A PENDING record exists and the tool is UNSAFE.

    The run stops and a human resolves it. This is the intended behaviour, not a
    limitation: the alternatives are double-charging a customer or throwing away the
    run, and neither is ours to choose silently.
    """


@dataclass(frozen=True, slots=True)
class InvocationDecision:
    """The ledger's answer, plus the recorded result when there is one."""

    action: InvocationAction
    result: str | None = None
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class InvocationOutcome:
    """
    What a ledger row currently says, with no instruction attached.

    Distinct from `InvocationDecision` on purpose. A decision answers "should I execute?"
    and can only be produced by the party that intends to — it commits a PENDING row as
    part of asking. An outcome answers "has this happened yet?", which is what a loop
    waiting on a tool running in *another process* needs (D-023), and answering it must
    not claim the invocation.

    Reading a PENDING outcome means "someone is executing this right now". The waiter's
    correct response is to keep waiting: re-dispatching would duplicate the redelivery
    the broker already guarantees.
    """

    status: InvocationStatus
    result: str | None = None
    is_error: bool = False

    @property
    def is_terminal(self) -> bool:
        """
        True once the row has stopped changing — nobody is going to run this.

        Includes NEEDS_REVIEW, which is terminal without being a result: waiting longer
        would not produce one.
        """
        return self.status is not InvocationStatus.PENDING


def invocation_key(
    run_id: uuid.UUID,
    step_idx: int,
    tool_name: str,
    args: Mapping[str, Any],
) -> str:
    """
    The idempotency key for one tool call.

    Four components, and each earns its place. `run_id` and `step_idx` scope the key to
    one attempt at one position, so the same tool called at two different steps of a run
    is two invocations. `tool_name` and `args` mean that a *resumed* step which produces
    the same call matches the record, while a step where the model changes its mind
    produces a different key and genuinely re-executes.

    Note what is **not** in it: the `tool_use_id`. That is freshly generated by the
    provider on every response, so including it would make the key different on every
    replay and the ledger would never match anything — the mechanism would look like it
    was working and would deduplicate nothing.

    `sort_keys=True` is load-bearing. Python dict ordering follows insertion order, and
    JSON from the model arrives in whatever order the model emitted it, so the same
    arguments could otherwise hash two ways. `separators` removes whitespace for the
    same reason.
    """
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(f"{run_id}|{step_idx}|{tool_name}|{canonical}".encode()).hexdigest()
    return digest


def resolve_pending(effect_class: EffectClass) -> InvocationAction:
    """
    Decide what to do about a PENDING record found after a crash.

    This is the t1-t2 window, and the whole design exists to make this one call
    answerable. The tool author answered it at definition time, in a required field,
    rather than leaving the runtime to guess at 3am.
    """
    match effect_class:
        case EffectClass.READ_ONLY:
            # No side effect, so replaying costs a little time and nothing else.
            return InvocationAction.RE_EXECUTE
        case EffectClass.IDEMPOTENT_WRITE:
            # The downstream deduplicates on a natural key, so a second write collapses
            # into the first. This is a claim the tool author made; we are trusting it.
            return InvocationAction.RE_EXECUTE
        case EffectClass.UNSAFE:
            # Replay might double the effect. Stop and let a human decide.
            return InvocationAction.NEEDS_REVIEW
