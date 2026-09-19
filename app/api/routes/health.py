"""Operational endpoints: liveness now, readiness in v1."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.schemas.health import HealthResponse

# No `/v1` prefix. The versioned contract in plan.md §2 covers the API that clients
# code against; ops endpoints are consumed by Docker, Kubernetes and Prometheus, which
# must not be asked to follow an application version bump.
router = APIRouter(tags=["ops"])


@router.get("/healthz", response_model=HealthResponse, summary="Liveness")
async def healthz() -> HealthResponse:
    """
    Report that the process is alive.

    Responsibility: one boolean, cheaply. Does NOT check Postgres, Redis or the model
    weights — that is `/readyz`. Performs no IO at all, so it can never be the thing
    that is slow when everything else is slow.
    """
    return HealthResponse(status="ok", service="agentforge")
