"""
Unit tests for the two v0 tools.

The calculator tests are mostly security tests, because the interesting thing about a
tool that evaluates a model-supplied string is everything it refuses to do.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.adapters.clock import SystemClock
from app.core.ports import Clock
from app.core.runtime.errors import ToolExecutionFailed, ToolInputInvalid
from app.core.tools.base import EffectClass, ExecutionMode
from app.core.tools.builtin.calculator import calculator_tool
from app.core.tools.builtin.clock import clock_tool


class FrozenClock:
    """A `Clock` that never moves. Structural conformance, no inheritance."""

    def __init__(self, at: datetime) -> None:
        self._at = at
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._at

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


async def _run(expression: str) -> str:
    spec = calculator_tool()
    return await spec.handler(spec.validate_input({"expression": expression}))


# --- calculator: what it computes ----------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("2 + 2", "4"),
        ("(2 + 3) * 4", "20"),
        ("7 / 2", "3.5"),
        ("7 // 2", "3"),
        ("7 % 2", "1"),
        ("2 ** 10", "1024"),
        ("-5 + 3", "-2"),
        ("+5", "5"),
        ("1.5 * 2", "3.0"),
    ],
)
async def test_it_evaluates_arithmetic(expression: str, expected: str) -> None:
    assert await _run(expression) == expected


# --- calculator: what it refuses -----------------------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo pwned')",  # the reason this is not eval()
        "open('/etc/passwd').read()",
        "().__class__.__bases__",
        "[1, 2, 3]",
        "{'a': 1}",
        "lambda: 1",
        "x + 1",  # a name
        "abs(-1)",  # a call
        "2 if True else 3",
        "1 < 2",
        "'a' * 3",
    ],
)
async def test_it_refuses_everything_that_is_not_arithmetic(expression: str) -> None:
    """
    The allow-list, stated as tests.

    Every one of these is valid Python that `eval` would happily run. The expression
    arrives from the model, whose context holds retrieved documents and user text, so
    anything that can influence either could otherwise choose what this process
    executes. This is the test that makes that impossible rather than unlikely.
    """
    with pytest.raises(ToolExecutionFailed):
        await _run(expression)


async def test_a_huge_exponent_is_rejected_rather_than_computed() -> None:
    """
    `9 ** 9 ** 9` is not an error in Python — it is a process that stops responding.

    Arbitrary-precision integers make this a denial of service with no exception to
    catch, so the only defence is refusing before evaluating.
    """
    with pytest.raises(ToolExecutionFailed, match="Exponent"):
        await _run("9 ** 999")


async def test_division_by_zero_is_a_tool_failure_not_a_crash() -> None:
    """
    The model can recover from this — it gets told, and tries something else.

    That is the difference between ToolExecutionFailed and an unexpected exception: one
    is a step the loop reports back, the other is a bug the loop must not swallow.
    """
    with pytest.raises(ToolExecutionFailed, match="Division by zero"):
        await _run("1 / 0")


async def test_a_complex_result_is_refused() -> None:
    """`(-1) ** 0.5` returns a complex number in Python, silently. It is not an answer."""
    with pytest.raises(ToolExecutionFailed, match="not a real number"):
        await _run("(-1) ** 0.5")


async def test_an_overflowing_result_is_refused() -> None:
    """`1e308 * 10` returns `inf` without raising. Handing that to the model is worse."""
    with pytest.raises(ToolExecutionFailed, match="finite"):
        await _run("1e308 * 10")


async def test_booleans_are_not_numbers() -> None:
    """`bool` subclasses `int`, so `True + 1` would otherwise quietly evaluate to 2."""
    with pytest.raises(ToolExecutionFailed):
        await _run("True + 1")


def test_an_over_long_expression_is_rejected_by_the_schema_not_the_parser() -> None:
    """
    The bound lives in the input model, so the model is *told* about it in the schema
    rather than discovering it by being rejected.
    """
    spec = calculator_tool()

    assert spec.json_schema()["properties"]["expression"]["maxLength"] == 200
    with pytest.raises(ToolInputInvalid):
        spec.validate_input({"expression": "1+" * 200})


def test_syntax_errors_come_back_as_something_the_model_can_fix() -> None:
    spec = calculator_tool()

    with pytest.raises(ToolExecutionFailed, match="not a valid expression"):
        asyncio.run(spec.handler(spec.validate_input({"expression": "2 +"})))


def test_calculator_is_read_only_and_inline() -> None:
    spec = calculator_tool()

    assert spec.effect_class is EffectClass.READ_ONLY
    assert spec.execution is ExecutionMode.INLINE


# --- now -----------------------------------------------------------------------------


async def test_now_reads_the_injected_clock() -> None:
    """
    The payoff of the Clock port: an exact assertion, no sleeping, no flakiness.

    A tool that called `datetime.now()` directly could only be tested by asserting the
    result is "close to" the current time, which is a test that passes for the wrong
    reasons and fails on a slow machine.
    """
    frozen = FrozenClock(datetime(2026, 9, 19, 12, 30, 0, tzinfo=UTC))
    spec = clock_tool(frozen)

    result = await spec.handler(spec.validate_input({}))

    assert result == "2026-09-19T12:30:00+00:00"


def test_now_takes_no_arguments_and_still_produces_a_valid_schema() -> None:
    schema = clock_tool(FrozenClock(datetime.now(UTC))).json_schema()

    assert schema["type"] == "object"
    assert schema.get("properties", {}) == {}
    assert "required" not in schema


def test_the_system_clock_satisfies_the_port_and_returns_aware_utc() -> None:
    """
    A naive datetime is the most common way timestamps go wrong — it compares and
    serialises incorrectly, and nothing raises when it does.
    """
    clock: Clock = SystemClock()

    moment = clock.now()

    assert moment.tzinfo is not None
    assert moment.utcoffset() == UTC.utcoffset(None)
