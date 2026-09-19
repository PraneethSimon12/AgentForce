"""
Unit tests for the single configuration read.

These are cheap tests guarding an expensive failure: the run limits in `Settings` are
the cost guardrails of CLAUDE.md §7, and a guardrail that can itself be misconfigured
is not a guardrail.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.settings import Settings

# `_env_file=None` on every construction: without it these tests read whatever .env
# happens to sit in the working directory, and start passing or failing based on the
# developer's local secrets. A unit test must depend on nothing outside its own body.


def test_defaults_match_the_documented_env_template() -> None:
    settings = Settings(_env_file=None)

    assert settings.llm_model == "claude-opus-5"
    assert settings.judge_model == "claude-haiku-4-5"
    assert settings.effort == "medium"
    assert settings.max_steps == 12
    assert settings.run_token_budget == 120_000
    assert settings.max_tool_retries == 2
    assert settings.anthropic_api_key is None


def test_environment_variables_are_read_through_their_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env var is `AGENTFORGE_MAX_STEPS`; the attribute is `max_steps`."""
    monkeypatch.setenv("AGENTFORGE_MAX_STEPS", "3")
    monkeypatch.setenv("AGENTFORGE_MODEL", "claude-haiku-4-5")

    settings = Settings(_env_file=None)

    assert settings.max_steps == 3
    assert settings.llm_model == "claude-haiku-4-5"


def test_fields_can_also_be_set_by_name_for_tests() -> None:
    """`populate_by_name=True` is what lets a test inject config without the environment."""
    settings = Settings(_env_file=None, max_steps=4, run_token_budget=5_000)

    assert settings.max_steps == 4
    assert settings.run_token_budget == 5_000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_steps", 0),  # a run must take at least one step
        ("max_steps", 51),  # the ceiling: a typo'd extra digit must not reach the API
        ("run_token_budget", 999),  # below this no real run completes
        ("run_token_budget", 2_000_001),  # ~one month of budget in a single run
        ("step_timeout_seconds", 0),
        ("max_tool_retries", 6),  # D-009: retries are bounded deliberately
    ],
)
def test_out_of_range_limits_are_rejected(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_unknown_environment_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """.env carries variables for phases that have no code yet; that is not an error."""
    monkeypatch.setenv("AGENTFORGE_RRF_K", "60")

    assert Settings(_env_file=None).max_steps == 12


def test_the_api_key_does_not_leak_into_a_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    CLAUDE.md §7: a leaked Anthropic key is scraped from GitHub within minutes.

    `SecretStr` is the structural version of that rule — the key cannot reach a log
    line, an exception's repr or a Sentry payload by accident, only by calling
    `.get_secret_value()` on purpose.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key-000")

    settings = Settings(_env_file=None)

    assert settings.anthropic_api_key is not None
    assert "sk-ant-not-a-real-key-000" not in repr(settings)
    assert "sk-ant-not-a-real-key-000" not in str(settings.anthropic_api_key)
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-not-a-real-key-000"
