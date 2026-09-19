"""
Unit tests for the conversation vocabulary.

The round-trip tests here are the cheap guard on an expensive, silent failure: a thinking
block that comes back to the model even slightly altered invalidates the turn and the
prompt cache at the same time, and neither failure raises anything (CLAUDE.md §8).
"""

from __future__ import annotations

from app.core.runtime.messages import (
    LLMResponse,
    Message,
    OpaqueBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)


def test_an_opaque_block_round_trips_every_field_it_arrived_with() -> None:
    """
    A thinking block must go back to the model byte-identical, `signature` included.

    We never read these fields, which is exactly why they survive: there is no parsing
    step to lose them in.
    """
    raw = {
        "type": "thinking",
        "thinking": "The user wants the sum of 2 and 2.",
        "signature": "ErUBCkYIBRgCIkDxk9Lm0vQ2",
    }

    block = OpaqueBlock.model_validate(raw)

    assert block.model_dump() == raw


def test_an_opaque_block_survives_a_field_we_have_never_seen() -> None:
    """A block type from a future API version must replay, not explode."""
    raw = {"type": "some_block_type_from_2027", "payload": {"nested": [1, 2, 3]}}

    assert OpaqueBlock.model_validate(raw).model_dump() == raw


def test_tool_use_input_is_a_parsed_object_not_a_string() -> None:
    """
    Escaping inside tool-call JSON varies between models, so the loop never sees the
    serialised form — only the parsed object.
    """
    block = ToolUseBlock(id="toolu_01A", name="calculator", input={"expr": '2 + "2"'})

    assert block.input["expr"] == '2 + "2"'


def test_a_failed_tool_still_produces_a_result_block() -> None:
    """Dropping it leaves the model with an unanswered question and a malformed turn."""
    result = ToolResultBlock(
        tool_use_id="toolu_01A", content="ZeroDivisionError: division by zero", is_error=True
    )

    assert result.is_error is True
    assert result.tool_use_id == "toolu_01A"


def test_all_results_for_one_turn_live_in_a_single_user_message() -> None:
    """
    Two parallel tool calls produce one user message with two blocks, never two messages.

    Splitting them does not error — it silently teaches the model to stop calling tools
    in parallel, which is why this is asserted rather than trusted.
    """
    message = Message(
        role="user",
        content=[
            ToolResultBlock(tool_use_id="toolu_01A", content="4"),
            ToolResultBlock(tool_use_id="toolu_01B", content="2026-09-19T00:00:00Z"),
        ],
    )

    assert len(message.content) == 2


def test_usage_defaults_cache_counters_to_zero() -> None:
    """A provider that reports no cache fields must not crash the accounting."""
    usage = Usage(input_tokens=1200, output_tokens=64)

    assert usage.cache_read_input_tokens == 0


def test_stop_details_is_absent_unless_the_model_refused() -> None:
    response = LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="4")],
        usage=Usage(input_tokens=10, output_tokens=2),
        model="claude-opus-5",
    )

    assert response.stop_details is None


def test_a_mixed_assistant_turn_round_trips_in_order() -> None:
    """
    Thinking, then text, then a tool call — the real shape of a step.

    Order is part of the payload: the blocks must go back in the sequence they arrived.
    """
    original = [
        {"type": "thinking", "thinking": "I should compute this.", "signature": "sig-abc"},
        {"type": "text", "text": "Let me calculate that."},
        {"type": "tool_use", "id": "toolu_01A", "name": "calculator", "input": {"expr": "2+2"}},
    ]
    blocks = [
        OpaqueBlock.model_validate(original[0]),
        TextBlock.model_validate(original[1]),
        ToolUseBlock.model_validate(original[2]),
    ]

    message = Message(role="assistant", content=blocks)

    assert [b.model_dump() for b in message.content] == original
