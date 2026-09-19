"""
In-memory `RunStore` and `ToolLedger`. For unit tests, and only for unit tests.

These exist so the loop's tests run with no Docker (CLAUDE.md §4) while exercising
**the same loop code** that runs in production. The alternative — a separate in-memory
loop for tests and a durable one for real — is two implementations of the most important
control flow in the project, and they would drift.

What these fakes reproduce faithfully is the part the loop depends on for correctness:
`UNIQUE(run_id, idx)` raising on a duplicate step, and `UNIQUE(idempotency_key)`
arbitrating a tool invocation. What they do **not** reproduce is transaction atomicity,
lease expiry against a database clock, or `SKIP LOCKED` — which is exactly why the
Postgres implementations have their own integration tests. A fake that claimed to test
those would be testing itself.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.runtime.errors import RunNotFound, StepAlreadyCommitted
from app.core.runtime.idempotency import (
    InvocationAction,
    InvocationDecision,
    InvocationStatus,
    resolve_pending,
)
from app.core.runtime.messages import ContentBlock, Message, block_from_dict, blocks_to_wire
from app.core.runtime.state import (
    NewRun,
    RunOutcome,
    RunRecord,
    RunStatus,
    RunUsage,
    StepRecord,
)
from app.core.tools.base import EffectClass


@dataclass
class _StoredRun:
    record: RunRecord
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None


class InMemoryRunStore:
    """A `RunStore` backed by a dict. Single-process, no durability, no transactions."""

    def __init__(self) -> None:
        self._runs: dict[uuid.UUID, _StoredRun] = {}
        self._by_key: dict[tuple[str, str], uuid.UUID] = {}

    async def create(self, spec: NewRun) -> RunRecord:
        if spec.idempotency_key is not None:
            existing = self._by_key.get((spec.tenant_id, spec.idempotency_key))
            if existing is not None:
                return self._runs[existing].record

        record = RunRecord(
            id=uuid.uuid4(),
            agent=spec.agent,
            status=RunStatus.QUEUED,
            input=spec.input,
            limits=spec.limits,
            effort=spec.effort,
            prompt_name=spec.prompt_name,
            prompt_version=spec.prompt_version,
            model=spec.model,
            usage=RunUsage(),
            messages=(),
            steps=(),
            tenant_id=spec.tenant_id,
            parent_run_id=spec.parent_run_id,
        )
        self._runs[record.id] = _StoredRun(record=record)
        if spec.idempotency_key is not None:
            self._by_key[(spec.tenant_id, spec.idempotency_key)] = record.id
        return record

    async def load(self, run_id: uuid.UUID) -> RunRecord:
        stored = self._runs.get(run_id)
        if stored is None:
            raise RunNotFound(f"No run with id {run_id}.")
        return stored.record

    async def commit_step(
        self, run_id: uuid.UUID, step: StepRecord, messages: Sequence[Message]
    ) -> None:
        stored = self._runs.get(run_id)
        if stored is None:
            raise RunNotFound(f"No run with id {run_id}.")
        if any(existing.idx == step.idx for existing in stored.record.steps):
            # Reproduces UNIQUE(run_id, idx). The loop's recovery path depends on this
            # raising, so a fake that silently overwrote would hide the bug it exists
            # to surface.
            raise StepAlreadyCommitted(f"Step {step.idx} of run {run_id} is already committed.")

        # Round-tripped through the wire form even here, so a block that would not
        # survive JSONB does not survive the fake either.
        replayed = tuple(
            Message(role=m.role, content=[_rehydrate(b) for b in blocks_to_wire(m.content)])
            for m in messages
        )
        usage = stored.record.usage.plus(step.usage)
        stored.record = _replace(
            stored.record,
            status=RunStatus.RUNNING,
            steps=(*stored.record.steps, step),
            messages=(*stored.record.messages, *replayed),
            usage=usage,
        )

    async def finish(self, run_id: uuid.UUID, outcome: RunOutcome) -> None:
        stored = self._runs[run_id]
        stored.lease_owner = None
        stored.lease_expires_at = None
        stored.record = _replace(
            stored.record,
            status=outcome.status,
            answer=outcome.answer,
            error_code=outcome.error_code,
            error_message=outcome.error_message,
            lease_owner=None,
            lease_expires_at=None,
        )

    async def claim(self, run_id: uuid.UUID, owner: str, ttl_seconds: int) -> RunRecord | None:
        stored = self._runs.get(run_id)
        if stored is None:
            raise RunNotFound(f"No run with id {run_id}.")
        if stored.record.status.is_terminal:
            return None
        now = datetime.now(UTC)
        held = stored.lease_expires_at is not None and stored.lease_expires_at > now
        if held and stored.lease_owner != owner:
            return None
        stored.lease_owner = owner
        stored.lease_expires_at = now + timedelta(seconds=ttl_seconds)
        stored.record = _replace(stored.record, status=RunStatus.RUNNING, lease_owner=owner)
        return stored.record

    async def claim_next_reclaimable(self, owner: str, ttl_seconds: int) -> RunRecord | None:
        for run_id, stored in self._runs.items():
            if stored.record.status.is_terminal:
                continue
            expires = stored.lease_expires_at
            if expires is None or expires <= datetime.now(UTC):
                return await self.claim(run_id, owner, ttl_seconds)
        return None

    async def renew(self, run_id: uuid.UUID, owner: str, ttl_seconds: int) -> bool:
        stored = self._runs[run_id]
        if stored.lease_owner != owner:
            return False
        if stored.lease_expires_at is None or stored.lease_expires_at <= datetime.now(UTC):
            return False
        stored.lease_expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        return True

    async def release(
        self, run_id: uuid.UUID, owner: str, *, status: RunStatus = RunStatus.PAUSED
    ) -> None:
        stored = self._runs[run_id]
        if stored.lease_owner != owner:
            return
        stored.lease_owner = None
        stored.lease_expires_at = None
        stored.record = _replace(stored.record, status=status, lease_owner=None)

    # --- test affordances ------------------------------------------------------------

    def expire_lease(self, run_id: uuid.UUID) -> None:
        """Simulate the worker holding this run dying. Tests only."""
        self._runs[run_id].lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)


@dataclass
class _Invocation:
    status: InvocationStatus
    effect_class: EffectClass
    result: str | None = None
    is_error: bool = False


@dataclass
class InMemoryToolLedger:
    """A `ToolLedger` backed by a dict, keyed the same way the table is."""

    rows: dict[str, _Invocation] = field(default_factory=dict)
    executions: list[str] = field(default_factory=list)
    """Every key the ledger authorised for execution. The exactly-once assertion."""

    async def begin(
        self,
        run_id: uuid.UUID,
        step_idx: int,
        tool_name: str,
        effect_class: EffectClass,
        key: str,
    ) -> InvocationDecision:
        existing = self.rows.get(key)
        if existing is None:
            self.rows[key] = _Invocation(status=InvocationStatus.PENDING, effect_class=effect_class)
            return InvocationDecision(action=InvocationAction.EXECUTE)
        if existing.status is InvocationStatus.PENDING:
            return InvocationDecision(action=resolve_pending(effect_class))
        return InvocationDecision(
            action=InvocationAction.REPLAY,
            result=existing.result,
            is_error=existing.is_error,
        )

    async def complete(self, key: str, result: str, *, is_error: bool) -> None:
        row = self.rows[key]
        row.status = InvocationStatus.FAILED if is_error else InvocationStatus.SUCCEEDED
        row.result = result
        row.is_error = is_error
        self.executions.append(key)


def _rehydrate(raw: dict[str, Any]) -> ContentBlock:
    return block_from_dict(raw)


def _replace(record: RunRecord, **changes: Any) -> RunRecord:
    """`dataclasses.replace` for a frozen dataclass, named so the intent is obvious."""
    return dataclasses.replace(record, **changes)
