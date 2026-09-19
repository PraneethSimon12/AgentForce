"""
Unit tests for the Anthropic adapter. No network — a stub client stands in for the SDK.

Two things get tested here and they are equally important. One is the **response**
translation, which is where a mangled thinking block would come from. The other is the
**request** shape, which is where prompt caching either works or silently does not.

These are unit tests because the SDK's own types are constructed directly. The live
smoke test that proves this matches reality is in `tests/integration/`, opt-in, because
it spends money.
"""

from __future__ import annotations

from typing import Any, cast

import anthropic
import httpx2 as httpx
import pytest
from anthropic.types import Message as SDKMessage

from app.adapters.llm.anthropic_client import AnthropicClient
from app.core.runtime.errors import LLMRequestError, LLMTransportError
from app.core.runtime.messages import (
    Message,
    OpaqueBlock,
    TextBlock,
    ToolSchema,
    ToolUseBlock,
)

REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


class StubMessages:
    """Captures the request and returns (or raises) a prepared outcome."""

    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> object:
        self.kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class StubAnthropic:
    def __init__(self, outcome: object) -> None:
        self.messages = StubMessages(outcome)


def build_client(outcome: object) -> tuple[AnthropicClient, StubMessages]:
    stub = StubAnthropic(outcome)
    client = AnthropicClient(
        api_key=None,
        model="claude-opus-5",
        client=cast(anthropic.AsyncAnthropic, stub),
    )
    return client, stub.messages


def sdk_response(
    content: list[dict[str, Any]],
    *,
    stop_reason: str = "end_turn",
    usage: dict[str, Any] | None = None,
) -> SDKMessage:
    return SDKMessage.model_validate(
        {
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5",
            "content": content,
            "stop_reason": stop_reason,
            "usage": (usage or {"input_tokens": 100, "output_tokens": 10}),
        }
    )


async def complete(client: AnthropicClient) -> Any:
    return await client.complete(
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        system="You are a test agent.",
        tools=[ToolSchema(name="calculator", description="Adds.", input_schema={"type": "object"})],
        max_tokens=1024,
        effort="medium",
    )


# --- Response translation ------------------------------------------------------------


async def test_text_and_tool_use_blocks_are_given_structure() -> None:
    client, _ = build_client(
        sdk_response(
            [
                {"type": "text", "text": "Let me compute that."},
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "calculator",
                    "input": {"expression": "2+2"},
                },
            ],
            stop_reason="tool_use",
        )
    )

    response = await complete(client)

    assert isinstance(response.content[0], TextBlock)
    tool_use = response.content[1]
    assert isinstance(tool_use, ToolUseBlock)
    assert tool_use.id == "toolu_01"
    assert tool_use.input == {"expression": "2+2"}


async def test_a_thinking_block_becomes_opaque_and_keeps_its_signature() -> None:
    """
    The block we must never mangle is the one we never parse (D-014).

    `signature` is what makes the turn valid when replayed; losing it invalidates the
    turn and the prompt cache at once, and neither failure raises.
    """
    client, _ = build_client(
        sdk_response(
            [
                {"type": "thinking", "thinking": "2 plus 2 is 4.", "signature": "abc123"},
                {"type": "text", "text": "4"},
            ]
        )
    )

    response = await complete(client)

    thinking = response.content[0]
    assert isinstance(thinking, OpaqueBlock)
    assert thinking.model_dump() == {
        "type": "thinking",
        "thinking": "2 plus 2 is 4.",
        "signature": "abc123",
    }


async def test_cache_counters_are_carried_through() -> None:
    """
    `cache_read_input_tokens` is the prompt-cache health signal (CLAUDE.md §7).

    Zero across repeated runs means something is invalidating the prefix, and that shows
    up on the bill before it shows up anywhere else — but only if the number survives
    translation.
    """
    client, _ = build_client(
        sdk_response(
            [{"type": "text", "text": "hi"}],
            usage={
                "input_tokens": 50,
                "output_tokens": 10,
                "cache_creation_input_tokens": 1_200,
                "cache_read_input_tokens": 3_400,
            },
        )
    )

    response = await complete(client)

    assert response.usage.cache_read_input_tokens == 3_400
    assert response.usage.cache_creation_input_tokens == 1_200


async def test_an_unrecognised_stop_reason_is_refused_rather_than_guessed() -> None:
    """
    Unknown *block types* are carried through opaquely; an unknown *stop reason* is not.

    It is the field the loop branches on, so treating a new terminal condition as an
    ordinary answer is the failure mode — and it would be silent.
    """
    raw = sdk_response([{"type": "text", "text": "hi"}])
    object.__setattr__(raw, "stop_reason", "some_future_reason")
    client, _ = build_client(raw)

    with pytest.raises(LLMRequestError, match="Unrecognised stop_reason"):
        await complete(client)


# --- Request shape -------------------------------------------------------------------


async def test_the_system_prompt_carries_a_cache_breakpoint() -> None:
    """
    The breakpoint sits on `system`, which caches the tools too.

    Render order is tools, then system, then messages, and a breakpoint caches
    everything up to and including its own block — so one breakpoint here covers both
    of the byte-identical parts of every request.
    """
    client, stub = build_client(sdk_response([{"type": "text", "text": "hi"}]))

    await complete(client)

    system = stub.kwargs["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[0]["text"] == "You are a test agent."


async def test_adaptive_thinking_is_requested_not_a_token_budget() -> None:
    """`budget_tokens` is rejected with a 400 on this model family; effort replaces it."""
    client, stub = build_client(sdk_response([{"type": "text", "text": "hi"}]))

    await complete(client)

    assert stub.kwargs["thinking"] == {"type": "adaptive"}
    assert stub.kwargs["output_config"] == {"effort": "medium"}
    assert "budget_tokens" not in str(stub.kwargs)


async def test_effort_sits_inside_output_config_not_at_the_top_level() -> None:
    """A top-level `effort` is silently ignored, which is the expensive kind of wrong."""
    client, stub = build_client(sdk_response([{"type": "text", "text": "hi"}]))

    await complete(client)

    assert "effort" not in stub.kwargs
    assert stub.kwargs["output_config"]["effort"] == "medium"


# --- Error mapping -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (
            anthropic.RateLimitError(
                "slow down", response=httpx.Response(429, request=REQUEST), body=None
            ),
            LLMTransportError,
        ),
        (anthropic.APIConnectionError(message="reset", request=REQUEST), LLMTransportError),
        (
            anthropic.APIStatusError(
                "upstream", response=httpx.Response(503, request=REQUEST), body=None
            ),
            LLMTransportError,
        ),
        (
            anthropic.APIStatusError(
                "bad request", response=httpx.Response(400, request=REQUEST), body=None
            ),
            LLMRequestError,
        ),
        (
            anthropic.AuthenticationError(
                "no key", response=httpx.Response(401, request=REQUEST), body=None
            ),
            LLMRequestError,
        ),
    ],
)
async def test_provider_errors_are_mapped_to_retryable_or_not(
    raised: Exception, expected: type[Exception]
) -> None:
    """
    The only distinction that matters, and the entire reason for catching at all.

    A 429 should back off and try again. A 400 never will succeed, and retrying it
    spends money slowly while hiding the real bug. Collapsing these into one broad
    `except APIStatusError` would erase the difference (CLAUDE.md §4).
    """
    client, _ = build_client(raised)

    with pytest.raises(expected):
        await complete(client)


def test_a_missing_api_key_fails_where_the_key_is_actually_needed() -> None:
    """
    Not at import, and not at the first call. The unit suite runs on FakeLLM and must
    not require a key to exist, so the error belongs to the component that needs one.
    """
    with pytest.raises(LLMRequestError, match="ANTHROPIC_API_KEY"):
        AnthropicClient(api_key=None, model="claude-opus-5")
