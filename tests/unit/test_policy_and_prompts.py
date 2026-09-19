"""
Direct tests for the cost guardrails and the prompt loader.

The policy is tested here without a model, a tool or a conversation — which is the
reason it is a separate object from the loop. The most expensive bug this project can
have lives in these twenty lines, so they get exhaustive tests rather than incidental
coverage through the loop.
"""

from __future__ import annotations

import pytest

from app.adapters.prompts import PROMPTS_DIR, PromptNotFound, load_prompt
from app.core.runtime.policy import (
    MIN_VIABLE_OUTPUT_TOKENS,
    BudgetExceeded,
    LoopPolicy,
    RunLimits,
)
from app.core.runtime.state import RunUsage


def test_a_fresh_run_is_allowed_to_start() -> None:
    LoopPolicy(RunLimits(max_steps=3, token_budget=10_000)).check(RunUsage())


def test_the_step_cap_is_reached_not_exceeded() -> None:
    """
    At exactly `max_steps` the run stops. Off-by-one here is a real extra API call.
    """
    policy = LoopPolicy(RunLimits(max_steps=3, token_budget=1_000_000))

    policy.check(RunUsage(steps=2))
    with pytest.raises(BudgetExceeded, match="step cap"):
        policy.check(RunUsage(steps=3))


def test_the_token_budget_counts_input_and_output() -> None:
    """
    Input tokens dominate a multi-step run — the whole conversation is resent every
    step — so a budget that counted only output would be off by roughly an order of
    magnitude and would not bound spend at all.
    """
    policy = LoopPolicy(RunLimits(max_steps=100, token_budget=10_000))

    policy.check(RunUsage(steps=1, input_tokens=4_000, output_tokens=4_000))
    with pytest.raises(BudgetExceeded, match="token budget"):
        policy.check(RunUsage(steps=1, input_tokens=5_000, output_tokens=4_500))


def test_a_run_stops_when_the_headroom_is_too_small_to_be_useful() -> None:
    """
    Not thrift. A request issued with a tiny `max_tokens` gets truncated, and the
    dangerous truncation is mid-tool-call: a syntactically valid response with a broken
    intent. Stopping cleanly beats spending the last 200 tokens on an untrustworthy one.
    """
    policy = LoopPolicy(RunLimits(max_steps=100, token_budget=10_000))
    just_under = 10_000 - MIN_VIABLE_OUTPUT_TOKENS + 1

    with pytest.raises(BudgetExceeded):
        policy.check(RunUsage(steps=1, input_tokens=just_under))


def test_the_error_carries_the_numbers_the_wire_contract_needs() -> None:
    """plan.md §2.1: the message names the limit, its value, and the step it happened at."""
    policy = LoopPolicy(RunLimits(max_steps=2, token_budget=10_000))

    with pytest.raises(BudgetExceeded) as caught:
        policy.check(RunUsage(steps=2))

    assert caught.value.limit == 2
    assert caught.value.step_idx == 2
    assert caught.value.limit_name == "step cap"


def test_max_tokens_is_the_configured_ceiling_while_budget_allows() -> None:
    policy = LoopPolicy(RunLimits(token_budget=120_000, max_output_tokens=16_000))

    assert policy.max_tokens_for_next_step(RunUsage()) == 16_000


def test_max_tokens_shrinks_to_the_remaining_budget() -> None:
    """Caps the overshoot at one step's input tokens rather than an arbitrary amount."""
    policy = LoopPolicy(RunLimits(token_budget=120_000, max_output_tokens=16_000))

    remaining = policy.max_tokens_for_next_step(RunUsage(steps=1, input_tokens=115_000))

    assert remaining == 5_000


def test_a_client_cannot_raise_its_own_limits() -> None:
    """
    Limits are values held by the policy, not arguments to `check`.

    A budget a caller can widen is advisory, which is the same as absent — this is the
    structural reason the ceiling lives in configuration (plan.md §2.2).
    """
    policy = LoopPolicy(RunLimits(max_steps=1))

    assert policy.limits.max_steps == 1
    with pytest.raises(BudgetExceeded):
        policy.check(RunUsage(steps=1))


# --- Prompt loading ------------------------------------------------------------------


def test_the_agent_prompt_loads() -> None:
    prompt = load_prompt("agent", 1)

    assert "AgentForge" in prompt
    assert len(prompt) > 100


def test_prompt_line_endings_are_normalised() -> None:
    """
    Git checks text files out CRLF on Windows and LF in the container, so the same
    committed file would otherwise produce two byte sequences — and two different
    prompt-cache prefixes — between a laptop and production.
    """
    assert "\r" not in load_prompt("agent", 1)


def test_the_same_prompt_object_comes_back_every_time() -> None:
    """
    Identity, not just equality. The system prompt is the cache prefix; returning the
    identical object removes any chance of a per-call transformation creeping in.
    """
    assert load_prompt("agent", 1) is load_prompt("agent", 1)


def test_a_missing_prompt_names_what_is_available() -> None:
    with pytest.raises(PromptNotFound, match="agent.v1.txt"):
        load_prompt("agent", 99)


def test_prompts_live_in_files_not_in_code() -> None:
    """
    CLAUDE.md §4 / D-011: a prompt is a config artefact with a version. Changing one
    invalidates eval results exactly the way changing an algorithm does, so it has to be
    diffable in review and attributable in git blame.
    """
    assert (PROMPTS_DIR / "agent.v1.txt").is_file()
