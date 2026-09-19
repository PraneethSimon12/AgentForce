"""
The durability schema. Postgres is the source of truth for run state (D-003).

Three tables, and the shape of each is driven by one requirement: a process killed
between any two statements must leave the database in a state a different worker can
pick up correctly.

- **runs** — one row per run, mutated in place as the run progresses. The only mutable
  table here, and the only one that needs a lease.
- **run_steps** — append-only. One committed row per completed step. `UNIQUE(run_id,
  idx)` is the durability invariant: a replayed step cannot become a second row, it
  raises.
- **run_messages** — append-only. The conversation, blocks stored verbatim as JSONB, in
  order. This is what a resume replays.

Steps and messages are separate tables because they answer different questions. A step
is an accounting record — what it cost, how many attempts, what stopped it. A message is
payload that goes back to the model byte for byte. One step can produce two messages (the
assistant turn, then the tool results), and a resume needs the messages without caring
about the accounting.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base. Alembic autogenerate reads its metadata."""


def _utcnow() -> Any:
    """Server-side clock for row timestamps."""
    return func.now()


class Run(Base):
    """
    One agent run.

    Statuses are stored as `String`, not a native Postgres enum. A native enum gives
    database-level integrity but makes adding a value a migration that takes a lock on
    the type, and the values here are expected to grow (NEEDS_REVIEW arrives with the
    idempotency ledger). A varchar plus the `RunStatus` StrEnum in Python puts the
    validation where the code that branches on it already lives. See D-020.
    """

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # A sub-agent's run points at its parent (v4). Self-referential and nullable, so the
    # roster can slice a parent's budget without a second table.
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=True
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", server_default="default"
    )
    agent: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The run's *effective* limits, resolved and clamped at creation. Stored rather than
    # re-read from settings on resume: a run that resumes under a different budget than
    # it started with is a run whose cost ceiling silently moved, and the audit trail
    # would not show it.
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    effort: Mapped[str] = mapped_column(String(8), nullable=False)

    # D-011: the prompt version is recorded on the run, so a quality change can be traced
    # to the prompt edit that caused it. Same reason `model` is here and not assumed.
    prompt_name: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)

    # Every NOT NULL counter carries a *server* default as well as a Python one. The
    # Python default only applies when SQLAlchemy builds the INSERT; a raw SQL insert,
    # a backfill, or a future ALTER TABLE adding one of these to a populated table
    # would fail without the server side. The ORM is not the only writer a schema has
    # to survive.
    steps_taken: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cache_read_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # The durable half of the retry budget (D-009). A per-step counter held in memory
    # is reset by the very crash it is meant to bound, so the backstop has to be a row.
    retries_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # The lease (v1.4). Both columns move together: a worker owns the run only while its
    # name is here *and* the expiry is in the future. An expired lease is the signal that
    # a worker died, which is what makes recovery automatic rather than manual.
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Replaying `POST /v1/runs` with the same key returns the original run instead of
    # creating a second one. Unique per tenant, because keys are chosen by clients.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_utcnow()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    steps: Mapped[list[RunStep]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunStep.idx"
    )
    messages: Mapped[list[RunMessage]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunMessage.idx"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_runs_tenant_idempotency"),
        # Cursor pagination on (created_at, id) — offset pagination is wrong here because
        # rows are inserted while a client pages through them (plan.md §2.2).
        Index("ix_runs_created_at_id", "created_at", "id"),
        Index("ix_runs_status", "status"),
        # The recovery query: runs that are RUNNING with a dead lease. Partial, because
        # it is only ever asked about a handful of rows out of the whole table.
        Index(
            "ix_runs_reclaimable",
            "lease_expires_at",
            postgresql_where="status = 'RUNNING'",
        ),
        CheckConstraint("max_steps > 0", name="ck_runs_max_steps_positive"),
        CheckConstraint("token_budget > 0", name="ck_runs_token_budget_positive"),
    )


class RunStep(Base):
    """
    One completed step. Append-only; never updated.

    `UNIQUE(run_id, idx)` is the load-bearing constraint of the whole durability story.
    A resumed run that tries to commit a step it already committed does not quietly
    produce a duplicate row and a double-counted token bill — it raises, and the code
    above it treats that as "this step is already done, move on".
    """

    __tablename__ = "run_steps"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    idx: Mapped[int] = mapped_column(Integer, nullable=False)

    stop_reason: Mapped[str] = mapped_column(String(24), nullable=False)
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cache_read_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # D-009: retries live in a row, not a loop counter. A counter vanishes on crash and
    # is invisible in production; a column can be queried and alerted on.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    # Tool call records for this step, as JSONB. Denormalised deliberately: they are only
    # ever read together with their step, so a fourth table would buy a join and no
    # query we actually want to run.
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )

    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_utcnow()
    )

    run: Mapped[Run] = relationship(back_populates="steps")

    __table_args__ = (
        UniqueConstraint("run_id", "idx", name="uq_run_steps_run_idx"),
        CheckConstraint("idx >= 0", name="ck_run_steps_idx_non_negative"),
    )


class RunMessage(Base):
    """
    One message in the conversation, stored as raw blocks. Append-only.

    `content` is JSONB holding the block list exactly as it went over the wire, not a
    rendered string. That is what makes a resumed run byte-identical to the one it
    continues: thinking blocks and their signatures survive, and so does the prompt-cache
    prefix (D-014).
    """

    __tablename__ = "run_messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    idx: Mapped[int] = mapped_column(Integer, nullable=False)

    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)

    # Which step produced this message. Null for the opening user message, which exists
    # before any step has run.
    step_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_utcnow()
    )

    run: Mapped[Run] = relationship(back_populates="messages")

    __table_args__ = (
        UniqueConstraint("run_id", "idx", name="uq_run_messages_run_idx"),
        CheckConstraint("idx >= 0", name="ck_run_messages_idx_non_negative"),
    )


class ToolInvocation(Base):
    """
    The idempotency ledger: one row per tool call, written before the call happens.

    `UNIQUE(idempotency_key)` is what makes "did this already run?" answerable at all.
    The row is inserted PENDING *before* execution, so a crash leaves evidence that the
    attempt existed even though the side effect is outside this database.

    Not folded into `run_steps`, despite being one-to-one-ish with a step's tool calls,
    for one reason: it has to be committed **before** the step, and a step row cannot
    exist before the step is done. They have different lifetimes, so they are different
    rows.
    """

    __tablename__ = "tool_invocations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    step_idx: Mapped[int] = mapped_column(Integer, nullable=False)

    # hash(run_id, step_idx, tool_name, args). Deliberately excludes the tool_use_id,
    # which the provider regenerates on every response — including it would make the key
    # different on every replay and the ledger would deduplicate nothing while looking
    # like it worked.
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)

    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    effect_class: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_error: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_utcnow()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_tool_invocations_key"),
        Index("ix_tool_invocations_run", "run_id", "step_idx"),
    )
