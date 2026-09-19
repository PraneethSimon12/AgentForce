"""
The v0 exit criterion, against the real API.

    "Then the *same loop object* completes a real run against claude-opus-5 with only
    the adapter swapped. If that swap needs a single change inside core/, the boundary
    is wrong and we fix it before v1."  — plan.md, v0

That is what this file tests, and it is the only claim it makes. Everything else about
the loop is already covered by the unit suite against FakeLLM; repeating those
assertions here would only be paying money for the same information.

**Cost: roughly ₹0.85 per run** (~1,200 input and ~150 output tokens across two steps on
claude-opus-5, at $5/$25 per million). Opt-in: excluded from the default run by the
`integration` marker in pyproject.toml, and skipped outright without a key.

    pytest -m integration tests/integration/test_anthropic_smoke.py -s
"""

from __future__ import annotations

import os

import pytest

from app.adapters.clock import SystemClock
from app.adapters.llm.anthropic_client import AnthropicClient
from app.adapters.prompts import load_prompt
from app.core.runtime.loop import AgentLoop
from app.core.runtime.policy import LoopPolicy, RunLimits
from app.core.runtime.state import RunStatus
from app.core.tools.builtin.calculator import calculator_tool
from app.core.tools.builtin.clock import clock_tool
from app.core.tools.registry import ToolRegistry
from app.settings import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="needs a real ANTHROPIC_API_KEY; this test spends money",
    ),
]


async def test_the_same_loop_completes_a_real_run_with_only_the_adapter_swapped() -> None:
    settings = Settings()
    registry = ToolRegistry()
    registry.register(calculator_tool())
    registry.register(clock_tool(SystemClock()))

    # The only line that differs from the unit tests. Everything below is identical.
    llm = AnthropicClient(api_key=settings.anthropic_api_key, model=settings.llm_model)

    loop = AgentLoop(
        llm=llm,
        registry=registry,
        policy=LoopPolicy(
            RunLimits(
                max_steps=settings.max_steps,
                token_budget=settings.run_token_budget,
                max_output_tokens=settings.max_output_tokens,
            )
        ),
        system_prompt=load_prompt("agent", 1),
        effort=settings.effort,
    )

    outcome = await loop.run(
        "Use the calculator to work out 48271 * 3319, then tell me only the number."
    )

    print(f"\nanswer: {outcome.answer}")
    print(f"steps:  {[(s.idx, s.stop_reason) for s in outcome.steps]}")
    print(
        f"tokens: in={outcome.usage.input_tokens} out={outcome.usage.output_tokens} "
        f"cache_read={outcome.usage.cache_read_tokens}"
    )

    assert outcome.status is RunStatus.COMPLETED, outcome.error_message
    # It must have used the tool rather than doing the arithmetic itself — that is what
    # makes this a test of the loop and not of the model's mental arithmetic.
    assert any(call.name == "calculator" for step in outcome.steps for call in step.tool_calls)
    assert all(call.ok for step in outcome.steps for call in step.tool_calls)
    assert "160,211,449" in (outcome.answer or "") or "160211449" in (outcome.answer or "")


async def test_the_real_adapter_reports_cache_counters() -> None:
    """
    Records what prompt caching is actually doing, rather than assuming.

    Expected to be zero in v0 and that is not a bug: the minimum cacheable prefix is
    512-4096 tokens depending on the model, and one short system prompt plus two toy
    tool schemas is under it. Asserting `>= 0` rather than `> 0` is the honest
    assertion — the number is printed so `eval-report.md` can record the run, and this
    test becomes meaningful the moment the prefix grows past the threshold.
    """
    settings = Settings()
    registry = ToolRegistry()
    registry.register(calculator_tool())

    llm = AnthropicClient(api_key=settings.anthropic_api_key, model=settings.llm_model)
    loop = AgentLoop(
        llm=llm,
        registry=registry,
        policy=LoopPolicy(RunLimits(max_steps=2, token_budget=20_000)),
        system_prompt=load_prompt("agent", 1),
        effort="low",
    )

    outcome = await loop.run("Say the word 'ready' and nothing else.")

    print(f"\ncache_read_input_tokens across the run: {outcome.usage.cache_read_tokens}")
    assert outcome.usage.cache_read_tokens >= 0
    assert outcome.usage.input_tokens > 0
