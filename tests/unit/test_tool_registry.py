"""
Unit tests for the typed tool registry.

These cover resume claims 1.3 ("typed tool registry") and 1.4 ("Pydantic models
auto-generate the JSON schemas sent to the LLM"). Claim 1.4 is not "we call
model_json_schema() somewhere" — it is that the schema the model plans against and the
validator that checks its reply are generated from one definition and therefore cannot
disagree. That is what `test_a_constraint_reaches_the_model_and_the_validator_together`
actually demonstrates.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from app.core.runtime.errors import (
    DuplicateToolName,
    InvalidToolSpec,
    ToolInputInvalid,
    ToolNotFound,
)
from app.core.tools.base import EffectClass, ExecutionMode, ToolSpec
from app.core.tools.registry import ToolRegistry


class CalculatorInput(BaseModel):
    expression: str = Field(description="A Python arithmetic expression, e.g. '2 + 2'.")
    precision: int = Field(default=2, ge=0, le=10)


class ClockInput(BaseModel):
    timezone: str = Field(default="UTC")


async def _calculate(payload: CalculatorInput) -> str:
    return "4"


async def _now(payload: ClockInput) -> str:
    return "2026-09-19T00:00:00Z"


def calculator_spec(name: str = "calculator") -> ToolSpec[CalculatorInput]:
    return ToolSpec(
        name=name,
        description="Evaluate an arithmetic expression.",
        input_model=CalculatorInput,
        handler=_calculate,
        effect_class=EffectClass.READ_ONLY,
    )


def clock_spec(name: str = "now") -> ToolSpec[ClockInput]:
    return ToolSpec(
        name=name,
        description="Return the current time.",
        input_model=ClockInput,
        handler=_now,
        effect_class=EffectClass.READ_ONLY,
    )


# --- The schema the model is shown -------------------------------------------------


def test_the_input_schema_is_generated_from_the_pydantic_model() -> None:
    """Resume claim 1.4: this dict is what lands in the `tools` parameter."""
    schema = calculator_spec().to_schema()

    assert schema.name == "calculator"
    assert schema.input_schema == {
        "type": "object",
        "title": "CalculatorInput",
        "properties": {
            "expression": {
                "description": "A Python arithmetic expression, e.g. '2 + 2'.",
                "title": "Expression",
                "type": "string",
            },
            "precision": {
                "default": 2,
                "maximum": 10,
                "minimum": 0,
                "title": "Precision",
                "type": "integer",
            },
        },
        "required": ["expression"],
    }


def test_a_constraint_reaches_the_model_and_the_validator_together() -> None:
    """
    The point of the whole design, in one test.

    `precision` is declared once, with `ge=0, le=10`. That single declaration becomes
    `minimum`/`maximum` in the schema the model plans against *and* the rule that
    rejects the model's reply. There is no second place to update, so there is no way
    for the two to disagree — which is the bug this prevents: a schema advertising a
    constraint the handler does not enforce, or worse, the reverse.
    """
    spec = calculator_spec()

    prop = spec.json_schema()["properties"]["precision"]
    assert (prop["minimum"], prop["maximum"]) == (0, 10)

    with pytest.raises(ToolInputInvalid):
        spec.validate_input({"expression": "2+2", "precision": 99})


def test_a_nested_input_model_produces_defs_and_refs() -> None:
    """
    Documents real behaviour rather than an opinion: nested models become `$defs`.

    Worth pinning, because it is the shape that would break if we ever turn on the
    API's `strict` mode, which has stricter requirements about references.
    """

    class Location(BaseModel):
        city: str

    class NestedInput(BaseModel):
        where: Location

    async def _handler(payload: NestedInput) -> str:
        return ""

    spec = ToolSpec(
        name="nested",
        description="A tool with a nested input.",
        input_model=NestedInput,
        handler=_handler,
        effect_class=EffectClass.READ_ONLY,
    )

    schema = spec.json_schema()
    assert schema["properties"]["where"] == {"$ref": "#/$defs/Location"}
    assert "Location" in schema["$defs"]


# --- Validating what the model sends back -------------------------------------------


def test_validate_input_returns_the_typed_model() -> None:
    parsed = calculator_spec().validate_input({"expression": "2+2"})

    assert isinstance(parsed, CalculatorInput)
    assert parsed.precision == 2


def test_validation_failure_names_the_offending_field() -> None:
    """
    The detail goes back to the model in a tool_result, so it has to be actionable.

    A model that is told "expression: Field required" fixes its own call on the next
    step. One that is told "validation failed" cannot, and the run burns a step.
    """
    with pytest.raises(ToolInputInvalid) as caught:
        calculator_spec().validate_input({"precision": 2})

    assert "expression" in caught.value.detail
    assert caught.value.tool_name == "calculator"
    assert "\n" not in caught.value.detail


def test_a_json_string_is_rejected_rather_than_parsed() -> None:
    """
    Guards CLAUDE.md §8: tool inputs are parsed by the adapter with json.loads and
    arrive here as an object. If a raw string ever reaches this method, something
    upstream is string-handling JSON and needs fixing — so it must fail loudly here
    rather than be quietly re-parsed.
    """
    with pytest.raises(ToolInputInvalid):
        calculator_spec().validate_input('{"expression": "2+2"}')  # type: ignore[arg-type]


# --- Definition-time rejection -------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "",  # empty
        "has space",
        "has.dot",
        "emoji-🚀",
        "x" * 65,  # over the API's 64-character limit
    ],
)
def test_an_illegal_tool_name_is_rejected_at_definition_time(name: str) -> None:
    """The API would reject these. Failing here means failing at import, not at call."""
    with pytest.raises(InvalidToolSpec):
        calculator_spec(name=name)


def test_a_tool_without_a_description_is_rejected() -> None:
    """
    An undescribed tool is one the model cannot choose correctly.

    That failure is invisible — it shows up as the agent picking the wrong tool, not as
    an error — which is exactly why it is worth a hard check at definition time.
    """
    with pytest.raises(InvalidToolSpec):
        ToolSpec(
            name="calculator",
            description="   ",
            input_model=CalculatorInput,
            handler=_calculate,
            effect_class=EffectClass.READ_ONLY,
        )


def test_effect_class_has_no_default() -> None:
    """
    D-004: the tool author must state whether replay is safe. Omission is an error.

    A default would make the safe answer invisible and the dangerous one easy to forget,
    and the runtime has no way to work it out on its own at recovery time.
    """
    with pytest.raises(TypeError):
        ToolSpec(  # type: ignore[call-arg]
            name="calculator",
            description="Evaluate an arithmetic expression.",
            input_model=CalculatorInput,
            handler=_calculate,
        )


def test_execution_defaults_to_inline() -> None:
    """Getting this wrong is a performance problem, not a correctness one."""
    assert calculator_spec().execution is ExecutionMode.INLINE


def test_a_registered_tool_cannot_be_mutated() -> None:
    spec = calculator_spec()

    with pytest.raises(Exception):  # noqa: B017 — dataclasses raise FrozenInstanceError
        spec.name = "something_else"  # type: ignore[misc]


# --- The registry --------------------------------------------------------------------


def test_registering_two_tools_under_one_name_is_refused() -> None:
    """
    Silent overwrite would let the schema the model saw and the handler that runs come
    from different definitions — the drift the whole design exists to prevent.
    """
    registry = ToolRegistry()
    registry.register(calculator_spec())

    with pytest.raises(DuplicateToolName):
        registry.register(calculator_spec())


def test_an_unknown_tool_name_reports_what_was_available() -> None:
    registry = ToolRegistry()
    registry.register(calculator_spec())
    registry.register(clock_spec())

    with pytest.raises(ToolNotFound) as caught:
        registry.get("send_email")

    assert caught.value.available == ("calculator", "now")
    assert "calculator" in str(caught.value)


def test_schemas_are_ordered_by_name_whatever_the_registration_order() -> None:
    """
    The cache-prefix test, and the reason `schemas()` sorts.

    `tools` renders ahead of `system` and `messages`, so it is the front of the
    prompt-cache prefix. If two processes register the same tools in different orders —
    which is all it takes for an import to move during a refactor — they would send
    different bytes and neither would hit the other's cache, on every step of every run.
    Nothing would fail; the bill would just go up.
    """
    forwards = ToolRegistry()
    forwards.register(calculator_spec())
    forwards.register(clock_spec())

    backwards = ToolRegistry()
    backwards.register(clock_spec())
    backwards.register(calculator_spec())

    assert [s.name for s in forwards.schemas()] == ["calculator", "now"]
    assert forwards.schemas() == backwards.schemas()


def test_registry_reports_its_contents() -> None:
    registry = ToolRegistry()
    registry.register(calculator_spec())

    assert len(registry) == 1
    assert "calculator" in registry
    assert "send_email" not in registry
    assert registry.names() == ("calculator",)


def test_get_returns_the_spec_not_a_copy() -> None:
    """The loop needs the real handler, not a reconstruction of it."""
    registry = ToolRegistry()
    spec = calculator_spec()
    registry.register(spec)

    assert registry.get("calculator") is spec
