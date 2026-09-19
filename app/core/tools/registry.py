"""
The registry: the set of tools an agent may call, and the schemas the model is shown.

Hand-rolled on purpose (CLAUDE.md Rule 5). It is roughly forty lines, and those forty
lines are the subject of the project rather than plumbing worth a dependency.
"""

from __future__ import annotations

from typing import Any

from app.core.runtime.errors import DuplicateToolName, ToolNotFound
from app.core.runtime.messages import ToolSchema
from app.core.tools.base import ToolSpec


class ToolRegistry:
    """
    A name-to-tool mapping that knows how to describe itself to a model.

    Responsibility: registration and lookup. Does NOT execute tools (the loop does),
    does NOT decide which agent may call what (the roster does, in v4), and does NOT
    validate arguments itself — it hands back the ToolSpec that can.

    Not a module-level global, for the same reason settings are not (D-013): two tests
    in one process must be able to hold different tool sets, and a v4 sub-agent is given
    a registry restricted to its allowlist rather than the whole world.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self._tools: dict[str, ToolSpec[Any]] = {}

    def register(self, spec: ToolSpec[Any]) -> None:
        """
        Add a tool.

        Preconditions: no tool with this name is registered.
        Raises: DuplicateToolName. Silently overwriting would mean the schema the model
            was shown and the handler that actually runs could come from two different
            definitions — the precise drift this whole design exists to prevent.
        """
        if spec.name in self._tools:
            raise DuplicateToolName(f"A tool named {spec.name!r} is already registered.")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec[Any]:
        """
        Look up a tool by the name the model used.

        Raises: ToolNotFound. A model naming a tool outside the registry is a real case,
            not an impossible one — it is a step-level error the loop reports back as a
            failed tool_result, not a crash.
        """
        try:
            return self._tools[name]
        except KeyError:
            # `from None`: the KeyError is an implementation detail of the dict, and
            # chaining it adds noise to a traceback without adding information.
            raise ToolNotFound(name, self.names()) from None

    def schemas(self) -> list[ToolSchema]:
        """
        Return every tool's wire form, in a deterministic order.

        Sorted by name, and the sort is the point. The `tools` array is rendered ahead
        of `system` and `messages`, so it sits at the very front of the prompt-cache
        prefix: reorder it and every byte after it is invalidated, on every step of
        every run. Insertion order would make that depend on import order, which changes
        silently during a refactor. Sorting makes the same set of tools always produce
        the same bytes.
        """
        return [self._tools[name].to_schema() for name in self.names()]

    def names(self) -> tuple[str, ...]:
        """Return the registered tool names, sorted. For logging and error messages."""
        return tuple(sorted(self._tools))

    def __contains__(self, name: str) -> bool:
        """Support membership tests against a tool name."""
        return name in self._tools

    def __len__(self) -> int:
        """Number of registered tools."""
        return len(self._tools)
