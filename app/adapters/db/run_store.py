"""
The Postgres implementation of `RunStore`.

The whole file exists to make one sentence true: **a step is either fully committed or
it never happened.** Everything else — idempotent creation, replaying the conversation,
writing the terminal state — is bookkeeping around that guarantee.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.adapters.db.models import Run, RunMessage, RunStep
from app.adapters.db.session import transaction
from app.core.runtime.errors import RunNotFound, StepAlreadyCommitted
from app.core.runtime.messages import (
    Effort,
    Message,
    StopReason,
    Usage,
    block_from_dict,
    blocks_to_wire,
)
from app.core.runtime.state import (
    ErrorCode,
    NewRun,
    RunLimits,
    RunOutcome,
    RunRecord,
    RunStatus,
    RunUsage,
    StepRecord,
    ToolCallRecord,
)


class PostgresRunStore:
    """
    Structurally a `RunStore`. Holds a session factory, never a session.

    A store that held one long-lived session would serialise every run through a single
    transaction and would keep it open across an LLM call — which can take minutes. Each
    method opens the shortest transaction that does its job and closes it.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def create(self, spec: NewRun) -> RunRecord:
        """Record a run in QUEUED, or return the existing one for a repeated key."""
        run = Run(
            id=uuid.uuid4(),
            parent_run_id=spec.parent_run_id,
            tenant_id=spec.tenant_id,
            agent=spec.agent,
            status=RunStatus.QUEUED.value,
            input=spec.input,
            max_steps=spec.limits.max_steps,
            token_budget=spec.limits.token_budget,
            max_output_tokens=spec.limits.max_output_tokens,
            effort=spec.effort,
            prompt_name=spec.prompt_name,
            prompt_version=spec.prompt_version,
            model=spec.model,
            idempotency_key=spec.idempotency_key,
        )
        try:
            async with transaction(self._sessions) as session:
                session.add(run)
        except IntegrityError:
            # The unique constraint fired, so another request with this key won the
            # race. Ask the database what it kept rather than assuming — a check-then-
            # insert would have had a window between the two, which is the same
            # lost-update shape as a lease claimed with "check then act".
            if spec.idempotency_key is None:
                raise
            existing = await self._find_by_idempotency_key(spec.tenant_id, spec.idempotency_key)
            if existing is None:
                raise
            return existing

        return await self.load(run.id)

    async def load(self, run_id: uuid.UUID) -> RunRecord:
        """Load a run with its steps and its conversation, both in index order."""
        async with self._sessions() as session:
            run = await self._get(session, run_id)
            return _to_record(run)

    async def commit_step(
        self,
        run_id: uuid.UUID,
        step: StepRecord,
        messages: Sequence[Message],
    ) -> None:
        """
        Commit one step, its messages and the updated usage counters in one transaction.

        The ordering inside matters less than the fact that there is only one commit.
        Postgres gives us all-or-nothing across the three writes, which is precisely why
        the source of truth is a database and not Redis (D-003).
        """
        try:
            async with transaction(self._sessions) as session:
                run = await self._get(session, run_id, with_children=False)

                next_idx = await self._message_count(session, run_id)
                session.add(
                    RunStep(
                        run_id=run_id,
                        idx=step.idx,
                        stop_reason=step.stop_reason,
                        input_tokens=step.usage.input_tokens,
                        output_tokens=step.usage.output_tokens,
                        cache_read_tokens=step.usage.cache_read_input_tokens,
                        tool_calls=[_tool_call_to_wire(c) for c in step.tool_calls],
                    )
                )
                for offset, message in enumerate(messages):
                    session.add(
                        RunMessage(
                            run_id=run_id,
                            idx=next_idx + offset,
                            role=message.role,
                            content=blocks_to_wire(message.content),
                            step_idx=step.idx,
                        )
                    )

                run.steps_taken = step.idx + 1
                run.input_tokens += step.usage.input_tokens
                run.output_tokens += step.usage.output_tokens
                run.cache_read_tokens += step.usage.cache_read_input_tokens
                if run.status == RunStatus.QUEUED.value:
                    run.status = RunStatus.RUNNING.value
                    run.started_at = datetime.now(UTC)
        except IntegrityError as exc:
            # UNIQUE(run_id, idx). After a crash this is the answer to "did I already
            # commit this step?", not a bug — the caller treats it as "already done".
            raise StepAlreadyCommitted(
                f"Step {step.idx} of run {run_id} is already committed."
            ) from exc

    async def finish(self, run_id: uuid.UUID, outcome: RunOutcome) -> None:
        """Write the terminal state and release the lease."""
        async with transaction(self._sessions) as session:
            run = await self._get(session, run_id, with_children=False)
            run.status = outcome.status.value
            run.output = {"answer": outcome.answer} if outcome.answer is not None else None
            run.error_code = outcome.error_code.value if outcome.error_code else None
            run.error_message = outcome.error_message
            run.completed_at = datetime.now(UTC)
            # A terminal run holds no lease. Leaving one behind would make the recovery
            # query keep finding a run that has nothing left to do.
            run.lease_owner = None
            run.lease_expires_at = None

    # --- Leases ----------------------------------------------------------------------
    #
    # Every expiry comparison below uses `func.now()` — the *database's* clock, never the
    # worker's. Two workers with a few seconds of clock skew would otherwise disagree
    # about whether a lease had expired, and both would believe they owned the run. One
    # clock, in one place, removes the question.

    async def claim(self, run_id: uuid.UUID, owner: str, ttl_seconds: int) -> RunRecord | None:
        """
        Take the lease on a known run, or return None because someone else holds it.

        A single conditional UPDATE, not a SELECT followed by an UPDATE. The row lock is
        taken by the UPDATE itself, so there is no window between deciding the lease is
        free and taking it — which is exactly the lost-update bug that check-then-act
        produces, and the one CLAUDE.md §8 names.

        Returns None rather than raising: "someone else is already running this" is an
        ordinary outcome of a recovery scan, not an error.
        """
        async with transaction(self._sessions) as session:
            stmt = (
                update(Run)
                .where(
                    Run.id == run_id,
                    Run.status.in_(
                        [RunStatus.QUEUED.value, RunStatus.RUNNING.value, RunStatus.PAUSED.value]
                    ),
                    or_(Run.lease_owner.is_(None), Run.lease_expires_at < func.now()),
                )
                .values(
                    lease_owner=owner,
                    lease_expires_at=func.now() + timedelta(seconds=ttl_seconds),
                    status=RunStatus.RUNNING.value,
                    started_at=func.coalesce(Run.started_at, func.now()),
                )
                .returning(Run.id)
            )
            claimed = (await session.execute(stmt)).scalar_one_or_none()
        return None if claimed is None else await self.load(run_id)

    async def claim_next_reclaimable(self, owner: str, ttl_seconds: int) -> RunRecord | None:
        """
        Find any run nobody is working on and take it. The crash-recovery scan.

        `FOR UPDATE SKIP LOCKED` is what makes this safe to run on every worker at once:
        without it, ten workers polling would all block on the same first row and the
        pool would serialise. With it, each worker skips rows another worker has locked
        and takes the next one, so throughput scales with workers instead of collapsing.

        Oldest first, so a run abandoned by a crashed worker is picked up before newer
        work rather than starving behind it.
        """
        async with transaction(self._sessions) as session:
            candidate = (
                await session.execute(
                    select(Run.id)
                    .where(
                        Run.status.in_(
                            [
                                RunStatus.QUEUED.value,
                                RunStatus.RUNNING.value,
                                RunStatus.PAUSED.value,
                            ]
                        ),
                        or_(Run.lease_owner.is_(None), Run.lease_expires_at < func.now()),
                    )
                    .order_by(Run.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if candidate is None:
                return None
            await session.execute(
                update(Run)
                .where(Run.id == candidate)
                .values(
                    lease_owner=owner,
                    lease_expires_at=func.now() + timedelta(seconds=ttl_seconds),
                    status=RunStatus.RUNNING.value,
                    started_at=func.coalesce(Run.started_at, func.now()),
                )
            )
        return await self.load(candidate)

    async def renew(self, run_id: uuid.UUID, owner: str, ttl_seconds: int) -> bool:
        """
        Extend the lease, but only if we still hold it.

        The `lease_owner == owner` predicate is the important half. A worker that paused
        long enough for its lease to expire has *already* had the run taken from it, and
        must find that out here rather than by continuing to write steps alongside the
        new owner. Returns False in that case, and the loop stops.
        """
        async with transaction(self._sessions) as session:
            stmt = (
                update(Run)
                .where(
                    Run.id == run_id,
                    Run.lease_owner == owner,
                    Run.lease_expires_at > func.now(),
                )
                .values(lease_expires_at=func.now() + timedelta(seconds=ttl_seconds))
                .returning(Run.id)
            )
            return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def release(
        self, run_id: uuid.UUID, owner: str, *, status: RunStatus = RunStatus.PAUSED
    ) -> None:
        """
        Give up the lease deliberately, leaving the run resumable.

        The clean counterpart to a crash. A worker shutting down releases, so the run is
        picked up immediately rather than after the lease TTL expires — which is the
        difference between a rolling deploy costing nothing and costing one TTL per run
        in flight.
        """
        async with transaction(self._sessions) as session:
            await session.execute(
                update(Run)
                .where(Run.id == run_id, Run.lease_owner == owner)
                .values(lease_owner=None, lease_expires_at=None, status=status.value)
            )

    # --- internals -------------------------------------------------------------------

    async def _get(
        self, session: AsyncSession, run_id: uuid.UUID, *, with_children: bool = True
    ) -> Run:
        stmt = select(Run).where(Run.id == run_id)
        if with_children:
            # Eager-loaded, because the alternative is a lazy load firing inside async
            # code after the session has moved on — which raises somewhere unrelated.
            stmt = stmt.options(selectinload(Run.steps), selectinload(Run.messages))
        run = (await session.execute(stmt)).scalar_one_or_none()
        if run is None:
            raise RunNotFound(f"No run with id {run_id}.")
        return run

    async def _find_by_idempotency_key(self, tenant_id: str, key: str) -> RunRecord | None:
        async with self._sessions() as session:
            stmt = (
                select(Run)
                .where(Run.tenant_id == tenant_id, Run.idempotency_key == key)
                .options(selectinload(Run.steps), selectinload(Run.messages))
            )
            run = (await session.execute(stmt)).scalar_one_or_none()
            return _to_record(run) if run is not None else None

    @staticmethod
    async def _message_count(session: AsyncSession, run_id: uuid.UUID) -> int:
        """
        How many messages are already stored, which is the next message index.

        Counted rather than tracked in a column, for the same reason `next_step_idx` is
        derived: a counter and the rows it counts are two facts that can disagree after
        a crash, and only one of them is the truth.
        """
        stmt = select(func.count()).select_from(RunMessage).where(RunMessage.run_id == run_id)
        return int((await session.execute(stmt)).scalar_one())


def _tool_call_to_wire(call: ToolCallRecord) -> dict[str, Any]:
    return {
        "tool_use_id": call.tool_use_id,
        "name": call.name,
        "ok": call.ok,
        "detail": call.detail,
    }


def _tool_call_from_wire(raw: dict[str, Any]) -> ToolCallRecord:
    return ToolCallRecord(
        tool_use_id=raw["tool_use_id"],
        name=raw["name"],
        ok=raw["ok"],
        detail=raw.get("detail"),
    )


def _to_record(run: Run) -> RunRecord:
    """
    Rebuild the domain object from rows.

    Messages go through `block_from_dict` — the same function the provider adapter uses
    on a live response. One implementation means a replayed conversation cannot differ
    from the original one, which is the entire point of storing raw blocks (D-014).
    """
    return RunRecord(
        id=run.id,
        agent=run.agent,
        status=RunStatus(run.status),
        input=run.input,
        limits=RunLimits(
            max_steps=run.max_steps,
            token_budget=run.token_budget,
            max_output_tokens=run.max_output_tokens,
        ),
        effort=cast(Effort, run.effort),
        prompt_name=run.prompt_name,
        prompt_version=run.prompt_version,
        model=run.model,
        usage=RunUsage(
            steps=run.steps_taken,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            cache_read_tokens=run.cache_read_tokens,
        ),
        messages=tuple(
            Message(
                role=cast(Any, message.role),
                content=[block_from_dict(block) for block in message.content],
            )
            for message in run.messages
        ),
        steps=tuple(
            StepRecord(
                idx=step.idx,
                stop_reason=cast(StopReason, step.stop_reason),
                usage=Usage(
                    input_tokens=step.input_tokens,
                    output_tokens=step.output_tokens,
                    cache_read_input_tokens=step.cache_read_tokens,
                ),
                tool_calls=tuple(_tool_call_from_wire(c) for c in step.tool_calls),
            )
            for step in run.steps
        ),
        tenant_id=run.tenant_id,
        parent_run_id=run.parent_run_id,
        answer=(run.output or {}).get("answer"),
        error_code=ErrorCode(run.error_code) if run.error_code else None,
        error_message=run.error_message,
        lease_owner=run.lease_owner,
        lease_expires_at=run.lease_expires_at,
    )
