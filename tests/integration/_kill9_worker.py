"""
A worker process that exists to be killed.

Run as a subprocess by `test_kill9.py`, which destroys it at a chosen point in the
durability sequence and then checks what survived. It is a separate process rather than
inline test code because the point is a *real operating-system process death* — no
cleanup, no finally blocks, no async cancellation, no chance to flush anything. An
in-process simulation always runs some of your code on the way down, and that is exactly
the code a real kill skips.

    python -m tests.integration._kill9_worker <run_id> <mode>

Modes, named for the instant they stall at:

  slow_tool    — the tool pauses *after* its side effect and before returning, so the
                 ledger has not been completed. A kill here lands in the **t1-t2**
                 window: the ledger says PENDING and nothing in the system can know
                 whether the effect happened.

  slow_commit  — the store pauses inside `commit_step`, by which point the tool has run
                 and the ledger has been completed. A kill here lands in the **t2-t3**
                 window, which D-021 closes completely.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import Sequence

from app.adapters.clock import SystemClock
from app.adapters.db.run_store import PostgresRunStore
from app.adapters.db.session import build_engine, build_session_factory
from app.adapters.db.tool_ledger import PostgresToolLedger
from app.adapters.llm.fake_llm import FakeLLM, calls_tool, says
from app.core.runtime.loop import AgentLoop
from app.core.runtime.messages import Message
from app.core.runtime.state import StepRecord
from app.core.tools.registry import ToolRegistry
from app.settings import Settings
from tests.integration._kill9_tool import make_side_effect_tool

PAUSE_SECONDS = 30.0
"""Long enough that the parent always wins the race and kills us mid-pause."""


class SlowCommitStore(PostgresRunStore):
    """
    A store that stalls inside `commit_step`.

    This is how the test aims at one specific instant. By the time `commit_step` is
    running, the tool has executed and the ledger has been completed, so a kill here is
    unambiguously the t2-t3 window rather than a race between two possibilities.
    """

    async def commit_step(
        self, run_id: uuid.UUID, step: StepRecord, messages: Sequence[Message]
    ) -> None:
        await asyncio.sleep(PAUSE_SECONDS)
        await super().commit_step(run_id, step, messages)


async def main() -> None:
    run_id = uuid.UUID(sys.argv[1])
    mode = sys.argv[2]

    settings = Settings()
    engine = build_engine(settings.database_url)
    sessions = build_session_factory(engine)

    registry = ToolRegistry()
    registry.register(
        make_side_effect_tool(
            sessions,
            run_id,
            pause_seconds=PAUSE_SECONDS if mode == "slow_tool" else 0.0,
        )
    )

    store_cls = SlowCommitStore if mode == "slow_commit" else PostgresRunStore
    loop = AgentLoop(
        llm=FakeLLM(script=[calls_tool("side_effect", {"label": "once"}), says("done")]),
        registry=registry,
        store=store_cls(sessions),
        ledger=PostgresToolLedger(sessions),
        clock=SystemClock(),
        load_prompt=lambda _n, _v: "You are a test agent.",
        # Short, so the parent does not have to wait long for the dead worker's lease to
        # expire before a replacement can take the run over.
        lease_ttl_seconds=3,
    )

    await loop.run(run_id, owner=f"kill9-{mode}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
