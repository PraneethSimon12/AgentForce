"""Response DTOs for the operational endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """
    The body of `GET /healthz`.

    Deliberately says nothing about dependencies. Liveness answers exactly one
    question — "is this process alive and is its event loop still turning?" — and a
    liveness probe that also pings Postgres turns one outage into two, because an
    orchestrator restarts healthy API pods when the database has a bad minute.
    Dependency state belongs to `/readyz` (plan.md §2.6), which arrives in v1.
    """

    status: Literal["ok"] = "ok"
    service: str = Field(
        description="Which service answered, for when several sit behind one ingress."
    )
