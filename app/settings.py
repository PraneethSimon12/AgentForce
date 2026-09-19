"""
The one and only place environment variables are read.

CLAUDE.md §4 bans `os.getenv` everywhere else. Every other module either receives its
configuration as an argument or asks for `Settings` through dependency injection. That
is what lets a test construct a `Settings` with different values without mutating the
process environment — and what stops a config value from being read in two places and
drifting.

Fields are added phase by phase, not all at once. This file currently carries only what
v0 uses: the LLM identity and the cost guardrails. Postgres, Redis and retrieval settings
land in v1/v2/v3 when there is code that reads them. `.env.example` documents the full
eventual set; unknown variables are ignored here rather than rejected (`extra="ignore"`),
so a .env ahead of the code is not an error.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Imported, not redefined. `Effort` is part of the request vocabulary that core owns;
# config depends on the domain, never the other way round. Two copies of this literal
# would drift the day a new level is added.
from app.core.runtime.messages import Effort

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """
    Typed, immutable, validated process configuration.

    Immutable (`frozen=True`) on purpose: configuration that can be mutated at runtime
    cannot be reconstructed from a log line, which makes "why did that run behave
    differently?" unanswerable after the fact.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        # Fields are populated by their env-var alias in production and by their field
        # name in tests — `Settings(max_steps=3)` must work without touching os.environ.
        populate_by_name=True,
    )

    # --- LLM ------------------------------------------------------------------
    # Optional, because the v0 unit suite runs entirely against FakeLLM and must not
    # require a key to exist. The Anthropic adapter is what raises when it is missing —
    # that is the component that actually needs it, so that is where the error belongs.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")

    # Named `llm_model`, not `model`: pydantic reserves the `model_` prefix, and a field
    # called `model` collides with `model_config`/`model_dump` and emits a warning.
    llm_model: str = Field(default="claude-opus-5", validation_alias="AGENTFORGE_MODEL")
    judge_model: str = Field(default="claude-haiku-4-5", validation_alias="AGENTFORGE_JUDGE_MODEL")
    effort: Effort = Field(default="medium", validation_alias="AGENTFORGE_EFFORT")

    # --- Run limits -----------------------------------------------------------
    # These are the cost guardrails from CLAUDE.md §7, and the upper bounds are the
    # point: a stray extra zero in .env must not be able to 10x the bill. The loop
    # enforces them; these bounds stop the enforcer itself from being misconfigured.
    max_steps: int = Field(default=12, ge=1, le=50, validation_alias="AGENTFORGE_MAX_STEPS")
    step_timeout_seconds: float = Field(
        default=60.0, gt=0, le=600, validation_alias="AGENTFORGE_STEP_TIMEOUT_SECONDS"
    )
    run_token_budget: int = Field(
        default=120_000, ge=1_000, le=2_000_000, validation_alias="AGENTFORGE_RUN_TOKEN_BUDGET"
    )
    max_output_tokens: int = Field(
        default=16_000, ge=1_024, le=128_000, validation_alias="AGENTFORGE_MAX_OUTPUT_TOKENS"
    )
    max_tool_retries: int = Field(
        default=2, ge=0, le=5, validation_alias="AGENTFORGE_MAX_TOOL_RETRIES"
    )

    # --- Observability --------------------------------------------------------
    log_level: LogLevel = Field(default="INFO", validation_alias="LOG_LEVEL")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the process-wide settings, parsing the environment exactly once.

    Cached rather than constructed at import time: an import-time `Settings()` means
    importing any module requires a valid environment, which breaks test collection and
    makes the failure mode "ImportError during pytest startup" instead of a clear error
    at the point of use.

    Tests that need different values construct `Settings(...)` directly and pass it in;
    they never call this.
    """
    return Settings()
