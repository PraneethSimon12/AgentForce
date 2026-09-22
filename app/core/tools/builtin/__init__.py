"""
The default tool set, in one place, so every process builds the same one.

This exists because v1.7 introduced a second process that needs a registry. The API
executes inline tools; a Celery worker executes durable ones (D-023), and it can only do
that if it can resolve the same name to the same handler. Two registrations built by hand
in two modules would be a drift bug that shows up as `ToolNotFound` on a worker at 3am,
which is a bad place to discover a typo.

Explicit registration rather than a decorator that scans imports (D-017): the set of
tools an agent may call is a decision, and a decision belongs in a function you can read.
"""

from __future__ import annotations

from app.core.ports import Clock
from app.core.tools.builtin.calculator import calculator_tool
from app.core.tools.builtin.clock import clock_tool
from app.core.tools.registry import ToolRegistry

__all__ = ["build_default_registry", "calculator_tool", "clock_tool"]


def build_default_registry(clock: Clock) -> ToolRegistry:
    """
    Build the registry every process uses.

    Takes its dependencies as arguments rather than reaching for them, so the worker and
    the API each supply their own and a test supplies a frozen clock (D-018). The order
    of registration does not matter — `schemas()` sorts by name so the prompt-cache
    prefix is stable whatever this function does.
    """
    registry = ToolRegistry()
    registry.register(calculator_tool())
    registry.register(clock_tool(clock))
    return registry
