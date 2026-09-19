"""
The `now` tool: the first thing in this codebase that needs a dependency.

`calculator` is a pure function, so it is a module-level handler. `now` has to read a
clock, and a clock is IO — which means `core/` cannot reach for one. That is not a
limitation to work around; it is the boundary doing its job on the very first tool that
tests it.

The answer is a factory that closes over the `Clock` port. The `ToolSpec` is built with
whichever clock it is given, so a test injects a frozen one and asserts an exact
timestamp with no sleeping and no flakiness. Note that the handler signature never
changed to accommodate this — a closure was enough, which is why we have not added a
context parameter that every tool would have to accept whether it needed one or not.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.ports import Clock
from app.core.tools.base import EffectClass, ExecutionMode, ToolSpec


class NowInput(BaseModel):
    """
    No arguments.

    A tool with no parameters is a normal case, and the empty schema it produces is
    worth knowing works — it is the degenerate input to `model_json_schema()` and the
    one most likely to be handled badly somewhere downstream.
    """


def clock_tool(clock: Clock) -> ToolSpec[NowInput]:
    """
    Build the `now` tool against a given clock.

    READ_ONLY, with a wrinkle worth being precise about: READ_ONLY means *safe to
    re-execute*, not *deterministic*. Re-running this after a crash returns a later
    timestamp than the first run produced. That is fine here, because D-004 resolves a
    READ_ONLY tool by re-executing it and nothing downstream depends on the two calls
    agreeing — but a tool whose correctness depended on returning the same value twice
    would need the recorded result replayed instead, which is a different mechanism.

    INLINE: reading a clock is not work.
    """

    async def _now(payload: NowInput) -> str:
        # RFC 3339 UTC, per the wire contract (plan.md Part 2). `isoformat()` on an
        # aware datetime already emits `+00:00`, which is valid RFC 3339 — rewriting it
        # to `Z` by hand would be string surgery for no gain.
        return clock.now().isoformat()

    return ToolSpec(
        name="now",
        description=(
            "Return the current date and time in UTC, as an RFC 3339 timestamp. "
            "Use this whenever the answer depends on what time it is."
        ),
        input_model=NowInput,
        handler=_now,
        effect_class=EffectClass.READ_ONLY,
        execution=ExecutionMode.INLINE,
    )
