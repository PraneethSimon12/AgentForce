"""
Unit tests for the liveness endpoint.

Runs against the ASGI app in-process via httpx's ASGITransport — no uvicorn, no port,
no network. The v0 exit criterion is a green suite with no infrastructure at all, and
that starts here.
"""

from __future__ import annotations

import httpx

from app.main import create_app
from app.settings import Settings


async def test_healthz_reports_ok() -> None:
    app = create_app(settings=Settings(_env_file=None))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "agentforge"}


async def test_healthz_is_not_versioned() -> None:
    """Ops endpoints sit outside the `/v1` contract — probes must not track API versions."""
    app = create_app(settings=Settings(_env_file=None))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/healthz")

    assert response.status_code == 404


def test_create_app_does_not_share_state_between_apps() -> None:
    """Each call yields an independent app; that is what makes per-test config possible."""
    first = create_app(settings=Settings(_env_file=None, max_steps=3))
    second = create_app(settings=Settings(_env_file=None, max_steps=9))

    assert first.state.settings.max_steps == 3
    assert second.state.settings.max_steps == 9
