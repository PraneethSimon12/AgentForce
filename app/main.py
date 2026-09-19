"""
Application factory and lifespan.

CLAUDE.md §5: connection pools open and close here and nowhere else. A pool created at
import time, or lazily on first use, has no defined shutdown — which is how a redeploy
leaves half-open Postgres connections behind and the next boot hits max_connections.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import health
from app.settings import Settings, get_settings


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """
    Own the lifetime of every process-wide resource.

    Empty in v0 — there is nothing to open yet. It exists now so that the Postgres
    engine (v1), the Redis client (v2) and the embedding model (v3) each have one
    obvious home, rather than being created wherever they were first needed.

    Everything before `yield` runs at startup; everything after runs at shutdown, and
    runs even when startup raised, which is why teardown belongs here and not in a
    signal handler.
    """
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    """
    Build the ASGI application.

    A factory, not a module-level `FastAPI()`, so a test can build an app with a
    different `Settings` without mutating the environment or reaching into a cache.
    That is Dependency Inversion applied to configuration: the app depends on a
    `Settings` value handed to it, not on a global it reads for itself.

    Preconditions: none — safe to call repeatedly; each call yields an independent app.
    """
    settings = settings or get_settings()
    app = FastAPI(
        title="AgentForge",
        summary="A durable, resumable runtime for LLM agents.",
        lifespan=lifespan,
    )
    # Stored on the app, not a module global: two apps in one test session must not
    # share configuration. Handlers reach it via `request.app.state.settings`.
    app.state.settings = settings
    app.include_router(health.router)
    return app


# Uvicorn's entrypoint is `app.main:app` (docker-compose.yml, Dockerfile). The factory
# above is the real one; this is the single module-level instantiation it needs.
app = create_app()
