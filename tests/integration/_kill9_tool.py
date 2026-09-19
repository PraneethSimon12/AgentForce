"""
The tool whose side effect the kill-9 test counts.

Shared by the worker subprocess and the test, so both agree on exactly what a "side
effect" is here: a row written straight to Postgres, outside anything the runtime
controls.

That last part is the point. If the test counted executions using the ledger, it would
be asking the mechanism under test to grade its own work. Separate tables, written by
the tool itself, are ground truth that know nothing about idempotency keys.
"""

from __future__ import annotations

import asyncio
import uuid

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.tools.base import EffectClass, ExecutionMode, ToolSpec

CREATE_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS test_side_effects (
        id          bigserial PRIMARY KEY,
        run_id      uuid NOT NULL,
        label       text NOT NULL,
        applied_at  timestamptz NOT NULL DEFAULT now(),
        UNIQUE (run_id, label)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS test_side_effect_attempts (
        id          bigserial PRIMARY KEY,
        run_id      uuid NOT NULL,
        label       text NOT NULL,
        attempt_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
)
"""
Two tables, because two different claims need proving and they are not the same claim.

(A tuple rather than one string because asyncpg refuses multiple statements in a single
prepared statement — a small reminder that the driver, not just the database, is part of
the contract.)

`test_side_effects` counts **outcomes** — what the world ended up with. It has
`UNIQUE (run_id, label)`, and that constraint plus `ON CONFLICT DO NOTHING` is what makes
this tool honestly `IDEMPOTENT_WRITE` rather than a tool that merely says so. The effect
class is a promise that the *downstream* deduplicates; here the downstream is this
constraint. A tool declaring IDEMPOTENT_WRITE while doing a plain INSERT would be lying,
and the ledger would faithfully permit the replay that doubled the row.

`test_side_effect_attempts` has no unique constraint and counts **executions** — how many
times the handler body actually ran. A system can be correct with two attempts and one
outcome. It cannot be correct with two outcomes.
"""

INSERT_ATTEMPT = """
INSERT INTO test_side_effect_attempts (run_id, label) VALUES (:run_id, :label)
"""

INSERT_EFFECT = """
INSERT INTO test_side_effects (run_id, label) VALUES (:run_id, :label)
ON CONFLICT (run_id, label) DO NOTHING
"""


class SideEffectInput(BaseModel):
    label: str = Field(description="Natural key for this effect, so a replay can dedupe.")


def make_side_effect_tool(
    sessions: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    pause_seconds: float = 0.0,
    effect_class: EffectClass = EffectClass.IDEMPOTENT_WRITE,
) -> ToolSpec[SideEffectInput]:
    """
    Build the tool for one run, optionally pausing *after* the write.

    The run id arrives as a parameter and lives in the handler's closure — which is
    exactly the pattern D-018 chose for tool dependencies, rather than threading a
    context object through every handler signature.

    The pause sits between the side effect and the return, which is precisely the t1-t2
    window: the effect has happened, and the ledger has not yet been told.
    """

    async def handler(payload: SideEffectInput) -> str:
        async with sessions() as session, session.begin():
            await session.execute(text(INSERT_ATTEMPT), {"run_id": run_id, "label": payload.label})
            await session.execute(text(INSERT_EFFECT), {"run_id": run_id, "label": payload.label})
        if pause_seconds:
            await asyncio.sleep(pause_seconds)
        return f"effect {payload.label} applied"

    return ToolSpec(
        name="side_effect",
        description="Apply a side effect that the test can count.",
        input_model=SideEffectInput,
        handler=handler,
        effect_class=effect_class,
        execution=ExecutionMode.INLINE,
    )
