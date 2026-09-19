"""
What a tool *is*: one definition that produces both the schema and the validator.

The load-bearing idea of this file is that a tool's Pydantic input model is its JSON
Schema. `model_json_schema()` generates the `input_schema` the model plans against, and
the same class validates whatever the model sends back. There is one definition, so the
two cannot drift — which is the failure they would otherwise have: a schema promising
`expr: str` while the handler quietly expects `expression`, discovered at runtime, in
production, on a tool call that already cost money to produce.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError

from app.core.runtime.errors import InvalidToolSpec, ToolInputInvalid
from app.core.runtime.messages import ToolSchema

# The API's own constraint on tool names. Enforcing it here means an illegal name is a
# startup failure instead of a 400 on the first request that happens to include it.
_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class EffectClass(StrEnum):
    """
    What replaying this tool would do to the world. Required on every tool.

    This exists because of one unavoidable gap (D-004): the crash window between "the
    tool ran" and "we recorded that it ran". The side effect is not in our database, so
    no transaction covers both, and on resume we genuinely cannot tell whether it
    happened. The only component that can answer "is replaying this safe?" is the person
    who wrote the tool — so the design forces them to answer once, at definition time,
    in a required field, rather than leaving the runtime to guess at 3am.
    """

    READ_ONLY = "read_only"
    """No side effects. Replay freely."""

    IDEMPOTENT_WRITE = "idempotent_write"
    """Writes, but the downstream deduplicates on a natural key. Replay is safe."""

    UNSAFE = "unsafe"
    """Replay may double the effect. The run stops for review; never auto-retried."""


class ExecutionMode(StrEnum):
    """Where the tool runs, which decides who owns its failure."""

    INLINE = "inline"
    """In the API process, inside the step's timeout. For fast, cheap tools."""

    DURABLE = "durable"
    """Dispatched to Celery with an idempotency key (v1). For slow or expensive tools."""


# A handler receives the *validated* model, never a raw dict. By the time it runs, its
# argument has already been through the same schema the model planned against.
type ToolHandler[T: BaseModel] = Callable[[T], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class ToolSpec[TInput: BaseModel]:
    """
    One registered tool: its contract, its implementation, and its risk class.

    A frozen dataclass rather than a Pydantic model, because a ToolSpec is constructed
    in code and never parsed from JSON — there is no boundary to validate at, and making
    it a model would mean teaching Pydantic to carry a Callable field for no benefit.
    Frozen so a registered tool cannot be mutated after the model has been shown its
    schema.

    Generic in its input model so `handler` is checked against `input_model` statically:
    a handler whose parameter type does not match the declared model is a mypy error,
    not a runtime surprise on the first call.
    """

    name: str
    description: str
    input_model: type[TInput]
    handler: ToolHandler[TInput]
    effect_class: EffectClass
    execution: ExecutionMode = ExecutionMode.INLINE

    def __post_init__(self) -> None:
        """
        Reject a tool the API would reject, at import time rather than at call time.

        Preconditions: none.
        Raises: InvalidToolSpec if `name` is empty, longer than 64 characters, or
            contains anything outside [a-zA-Z0-9_-]; or if `description` is empty — an
            undescribed tool is one the model cannot choose correctly, which shows up as
            a quality problem rather than an error.
        """
        if not _TOOL_NAME.match(self.name):
            raise InvalidToolSpec(
                f"Tool name {self.name!r} must match {_TOOL_NAME.pattern} — the API "
                f"rejects anything else."
            )
        if not self.description.strip():
            raise InvalidToolSpec(f"Tool {self.name!r} has no description.")

    def json_schema(self) -> dict[str, Any]:
        """
        Return the JSON Schema for this tool's input, generated from its Pydantic model.

        Responsibility: generation only. Does NOT decide how the schema is ordered,
        cached or transmitted — the registry orders, the adapter sends.
        """
        return self.input_model.model_json_schema()

    def to_schema(self) -> ToolSchema:
        """Return the wire form of this tool: what one entry in `tools` contains."""
        return ToolSchema(
            name=self.name,
            description=self.description,
            input_schema=self.json_schema(),
        )

    def validate_input(self, raw: Mapping[str, Any]) -> TInput:
        """
        Validate one set of model-supplied arguments against this tool's input model.

        `raw` is the already-parsed `input` object from a tool_use block — parsed with
        json.loads by the adapter and never string-matched, because escaping inside
        tool-call JSON varies between models (CLAUDE.md §8).

        Preconditions: `raw` is a mapping, not a JSON string.
        Raises: ToolInputInvalid, carrying the validation detail, so the loop can send a
            tool_result with is_error=True and let the model correct itself. That is a
            recoverable step, not a failed run.
        """
        try:
            return self.input_model.model_validate(raw)
        except ValidationError as exc:
            raise ToolInputInvalid(self.name, _explain(exc)) from exc


def _explain(exc: ValidationError) -> str:
    """
    Compress a Pydantic ValidationError into one line the *model* can act on.

    `str(exc)` is written for a developer reading a traceback: multi-line, with a
    documentation URL. This goes into a tool_result block instead, where the reader is
    the model deciding how to fix its own call — so it needs the field path and the
    reason, and nothing else. Keeping it short also keeps it cheap, since it becomes
    part of the conversation for every subsequent step of the run.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors()
    )
