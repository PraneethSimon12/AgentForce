"""
A toy tool that is not quite a toy: arithmetic over a model-supplied expression.

The obvious implementation is `eval(expression)`, and it is a remote code execution
vulnerability. The string comes from the model, and the model's context contains
retrieved documents and user input — so anything that can influence either can choose
what this process executes. `eval("__import__('os').system('...')")` is one prompt
injection away, and no amount of "the model would not do that" makes it safe.

So we parse the expression into an AST and walk it, permitting one explicit set of node
types and nothing else. This is the general shape of every tool that accepts structured
input from a model: allow-list what you understand, reject the rest by default, and do
not rely on the model's good behaviour for a security property.
"""

from __future__ import annotations

import ast
import math
import operator
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from app.core.runtime.errors import ToolExecutionFailed
from app.core.tools.base import EffectClass, ExecutionMode, ToolSpec

_BINARY_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# Python integers are arbitrary precision, so `9 ** 9 ** 9` is not an error — it is a
# process that stops responding while it allocates. The cap turns a denial of service
# into a rejected tool call.
_MAX_EXPONENT = 64


class CalculatorInput(BaseModel):
    """
    Arguments for `calculator`.

    `max_length` is a real bound, not decoration: it caps parse cost, and it keeps the
    expression short enough that echoing it back in an error message cannot meaningfully
    grow the conversation. Because this model generates the schema, the model is told
    about the limit rather than discovering it by being rejected.
    """

    expression: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "An arithmetic expression over numbers, using + - * / // % ** and "
            "parentheses. Example: '(2 + 3) * 4'. Variables, function calls and "
            "names are not supported."
        ),
    )


def _evaluate(node: ast.expr) -> int | float:
    """
    Evaluate one AST node, or refuse.

    Responsibility: arithmetic only. Every branch here is an explicit permission; the
    final `raise` is the default, which is what makes this an allow-list rather than a
    blocklist. A blocklist of dangerous node types would be wrong for the usual reason —
    it has to be updated every time the language grows a feature.
    """
    if isinstance(node, ast.Constant):
        # `bool` is a subclass of `int`, so `True + 1` would otherwise evaluate to 2.
        # Arithmetic over booleans is never what the model meant.
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise ToolExecutionFailed(f"{node.value!r} is not a number.")
        return node.value

    if isinstance(node, ast.UnaryOp):
        unary = _UNARY_OPS.get(type(node.op))
        if unary is None:
            raise ToolExecutionFailed(f"{type(node.op).__name__} is not a supported operator.")
        return _finite(unary(_evaluate(node.operand)))

    if isinstance(node, ast.BinOp):
        binary = _BINARY_OPS.get(type(node.op))
        if binary is None:
            raise ToolExecutionFailed(f"{type(node.op).__name__} is not a supported operator.")
        left = _evaluate(node.left)
        right = _evaluate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_EXPONENT:
            raise ToolExecutionFailed(f"Exponent {right} exceeds the limit of {_MAX_EXPONENT}.")
        try:
            return _finite(binary(left, right))
        except ZeroDivisionError:
            raise ToolExecutionFailed("Division by zero.") from None
        except OverflowError:
            raise ToolExecutionFailed("The result is too large to represent.") from None

    raise ToolExecutionFailed(
        f"{type(node).__name__} is not allowed here — this tool evaluates arithmetic only."
    )


def _finite(value: Any) -> int | float:
    """
    Narrow an operator's result back to a real number, or refuse it.

    Not merely a type-checker appeasement. `(-1) ** 0.5` returns a *complex* number in
    Python, and `1e308 * 10` returns `inf` without raising. Both would otherwise be
    stringified and handed to the model as if they were answers.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ToolExecutionFailed("The result is not a real number.")
    if not math.isfinite(value):
        raise ToolExecutionFailed("The result is not a finite number.")
    return value


async def _calculate(payload: CalculatorInput) -> str:
    """Parse and evaluate. Raises ToolExecutionFailed for anything the model can fix."""
    try:
        tree = ast.parse(payload.expression, mode="eval")
    except SyntaxError as exc:
        raise ToolExecutionFailed(
            f"{payload.expression!r} is not a valid expression: {exc.msg}."
        ) from None
    return str(_evaluate(tree.body))


def calculator_tool() -> ToolSpec[CalculatorInput]:
    """
    Build the `calculator` tool.

    READ_ONLY because it has no side effects at all, so replaying it after a crash is
    free and always correct. INLINE because it completes in microseconds — dispatching
    it to Celery would cost more than running it.
    """
    return ToolSpec(
        name="calculator",
        description=(
            "Evaluate an arithmetic expression and return the result. "
            "Use this instead of doing arithmetic yourself whenever precision matters."
        ),
        input_model=CalculatorInput,
        handler=_calculate,
        effect_class=EffectClass.READ_ONLY,
        execution=ExecutionMode.INLINE,
    )
