"""
The Postgres idempotency ledger.

Two methods, each in its own transaction, in a deliberate order relative to the step
commit. The timeline and why it is shaped this way are in
`core/runtime/idempotency.py`; this file is just the storage.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.db.models import ToolInvocation
from app.adapters.db.session import transaction
from app.core.runtime.idempotency import (
    InvocationAction,
    InvocationDecision,
    InvocationStatus,
    resolve_pending,
)
from app.core.tools.base import EffectClass


class PostgresToolLedger:
    """Structurally a `ToolLedger`."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def begin(
        self,
        run_id: uuid.UUID,
        step_idx: int,
        tool_name: str,
        effect_class: EffectClass,
        key: str,
    ) -> InvocationDecision:
        """
        Insert a PENDING row, or read the one that is already there.

        Written as insert-first rather than check-then-insert. The check-then-insert
        version has a window between the SELECT finding nothing and the INSERT landing,
        and two workers racing through that window both conclude they should execute —
        which is the precise failure the ledger exists to prevent. Letting the unique
        constraint arbitrate means the database decides, atomically, and the loser reads
        the winner's row.
        """
        try:
            async with transaction(self._sessions) as session:
                session.add(
                    ToolInvocation(
                        run_id=run_id,
                        step_idx=step_idx,
                        idempotency_key=key,
                        tool_name=tool_name,
                        effect_class=effect_class.value,
                        status=InvocationStatus.PENDING.value,
                    )
                )
        except IntegrityError:
            return await self._decide_from_existing(key, effect_class)

        return InvocationDecision(action=InvocationAction.EXECUTE)

    async def complete(self, key: str, result: str, *, is_error: bool) -> None:
        """
        Record the outcome immediately after execution, before the step is committed.

        This is what closes the window between "the tool finished" and "the step was
        committed": a crash after this point finds SUCCEEDED and replays the recorded
        result rather than running the tool a second time.
        """
        status = InvocationStatus.FAILED if is_error else InvocationStatus.SUCCEEDED
        async with transaction(self._sessions) as session:
            await session.execute(
                update(ToolInvocation)
                .where(ToolInvocation.idempotency_key == key)
                .values(
                    status=status.value,
                    result=result,
                    is_error=is_error,
                    completed_at=datetime.now(UTC),
                )
            )

    async def _decide_from_existing(
        self, key: str, effect_class: EffectClass
    ) -> InvocationDecision:
        """Read the existing row and turn its status into an instruction."""
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(ToolInvocation).where(ToolInvocation.idempotency_key == key)
                )
            ).scalar_one()

        match InvocationStatus(row.status):
            case InvocationStatus.SUCCEEDED | InvocationStatus.FAILED:
                # Completed. Hand back exactly what happened last time — including a
                # failure, which is replayed rather than retried. Retrying a declared
                # failure would spend money to get the same answer.
                return InvocationDecision(
                    action=InvocationAction.REPLAY,
                    result=row.result,
                    is_error=row.is_error,
                )
            case InvocationStatus.PENDING:
                # The unanswerable window. The tool author already decided.
                return InvocationDecision(action=resolve_pending(effect_class))
