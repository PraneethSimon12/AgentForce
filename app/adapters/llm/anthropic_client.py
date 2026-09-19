"""
The real `LLMClient`: translation between AgentForge's vocabulary and the Anthropic SDK.

This file is the price of D-014 — `core/` owning its own message types — and keeping it
small is the check on whether that decision is still paying. If it grows past roughly a
hundred lines of translation, we are re-implementing the SDK rather than adapting it,
and the decision gets revisited.

Three things live here and nowhere else, each because it is a property of the transport
rather than of the conversation:

- **Prompt caching.** `core` hands over a plain system string; the `cache_control`
  breakpoint is attached here.
- **Which model.** The client is bound to one model at construction, so the eval
  harness's cheap judge is a second client rather than a parameter (`ports.py`).
- **Retryable versus not.** SDK exception types are mapped onto `LLMTransportError` and
  `LLMRequestError` so the loop can implement a retry budget without importing
  `anthropic` and without knowing `RateLimitError` exists.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import anthropic
from anthropic.types import (
    MessageParam,
    OutputConfigParam,
    TextBlockParam,
    ThinkingConfigAdaptiveParam,
    ToolParam,
)
from pydantic import SecretStr

from app.core.runtime.errors import LLMRequestError, LLMTransportError
from app.core.runtime.messages import (
    ContentBlock,
    Effort,
    LLMResponse,
    Message,
    OpaqueBlock,
    StopDetails,
    StopReason,
    TextBlock,
    ToolSchema,
    ToolUseBlock,
    Usage,
)


class AnthropicClient:
    """
    One model, one transport. Structurally an `LLMClient`; inherits from nothing.

    Note on retries: the SDK retries connection errors, 408, 409, 429 and 5xx twice by
    default, and that default is kept deliberately. D-009 counts it as the third layer
    beneath the per-step and per-run budgets — the point is not to have fewer layers but
    to know how many there are, because 2 x 3 x 3 attempts for one user action is how a
    rate limit becomes an outage.
    """

    def __init__(
        self,
        *,
        api_key: SecretStr | None,
        model: str,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        """
        Preconditions: `api_key` is present unless an already-configured `client` is
        supplied.

        Raises: LLMRequestError if there is no key. Raised here rather than at import or
        at first call, because this is the component that actually needs one — the unit
        suite runs entirely on FakeLLM and must not require a key to exist.
        """
        if client is None and api_key is None:
            raise LLMRequestError(
                "ANTHROPIC_API_KEY is not set. Unit tests use FakeLLM and need no key; "
                "anything that talks to the real API does."
            )
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key.get_secret_value() if api_key else None
        )
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self,
        *,
        messages: Sequence[Message],
        system: str,
        tools: Sequence[ToolSchema],
        max_tokens: int,
        effort: Effort,
    ) -> LLMResponse:
        """Send one request and translate the response. See `ports.LLMClient.complete`."""
        try:
            raw = await self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=_cached_system(system),
                messages=[_to_wire_message(m) for m in messages],
                tools=[_to_wire_tool(t) for t in tools],
                # Adaptive thinking, not a fixed `budget_tokens` — the latter is rejected
                # with a 400 on this model family. Depth is controlled by `effort`.
                thinking=ThinkingConfigAdaptiveParam(type="adaptive"),
                output_config=OutputConfigParam(effort=effort),
            )
        # Most specific first. Collapsing these into one `except APIStatusError` would
        # lose the only distinction that matters: a 429 should back off and try again,
        # a 400 never will succeed and retrying it spends money slowly (CLAUDE.md §4).
        except anthropic.RateLimitError as exc:
            raise LLMTransportError(f"Rate limited: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMTransportError(f"Connection failed: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMTransportError(f"Upstream {exc.status_code}: {exc}") from exc
            raise LLMRequestError(f"Request rejected ({exc.status_code}): {exc}") from exc

        return _from_wire_response(raw)


def _cached_system(system: str) -> list[TextBlockParam]:
    """
    Wrap the system prompt with a cache breakpoint.

    The request renders `tools`, then `system`, then `messages`, and a breakpoint caches
    everything up to and including the block it sits on. Putting it on the system block
    therefore covers the tool schemas as well — which is what we want, since both are
    byte-identical on every step of every run and the messages are not.

    Honest caveat for v0: the minimum cacheable prefix is 512-4096 tokens depending on
    the model, and our prompt plus two toy tools is well under that. This will not cache
    yet, and `usage.cache_read_input_tokens` will read zero for reasons that are not a
    bug. It starts paying once the system prompt and the tool set grow, and the metric
    is on the dashboard from v2 so we will see the moment it does.
    """
    return [TextBlockParam(type="text", text=system, cache_control={"type": "ephemeral"})]


def _to_wire_tool(tool: ToolSchema) -> ToolParam:
    return ToolParam(
        name=tool.name,
        description=tool.description,
        # The SDK types `input_schema` as a narrow TypedDict. Ours is generated by
        # `model_json_schema()`, which is correct JSON Schema but wider than that
        # shape describes, so the cast is the honest annotation rather than a
        # silenced error.
        input_schema=cast(Any, tool.input_schema),
    )


def _to_wire_message(message: Message) -> MessageParam:
    """
    Serialise one message back to the wire.

    `exclude_none=True` matters: our block models carry optional fields that were never
    in the original payload, and echoing `"signature": null` back into a thinking block
    is the kind of small difference that invalidates a turn and a cache entry at once.
    """
    return MessageParam(
        role=message.role,
        # Blocks are built dynamically from our own models, so the concrete union of
        # SDK block params cannot be expressed here without re-declaring all of them —
        # which is exactly the duplication D-014 exists to avoid.
        content=cast(Any, [block.model_dump(exclude_none=True) for block in message.content]),
    )


def _from_wire_response(raw: Any) -> LLMResponse:
    """
    Translate an SDK response into our own types.

    Only `text` and `tool_use` are given structure — everything else becomes an
    `OpaqueBlock` carrying exactly the fields it arrived with. That is what makes
    thinking blocks round-trip byte-identically and what makes a block type introduced
    after this code was written replay unharmed rather than crash (D-014).
    """
    content: list[ContentBlock] = []
    for block in raw.content:
        payload = block.model_dump(exclude_none=True)
        kind = payload.get("type")
        if kind == "text":
            content.append(TextBlock(text=payload["text"]))
        elif kind == "tool_use":
            content.append(
                ToolUseBlock(id=payload["id"], name=payload["name"], input=payload["input"])
            )
        else:
            content.append(OpaqueBlock.model_validate(payload))

    stop_details = None
    # Populated only for a refusal; null for every other stop reason, so it is guarded
    # rather than assumed.
    if raw.stop_reason == "refusal" and getattr(raw, "stop_details", None) is not None:
        stop_details = StopDetails(
            type=raw.stop_details.type,
            category=getattr(raw.stop_details, "category", None),
            explanation=getattr(raw.stop_details, "explanation", None),
        )

    usage = raw.usage
    return LLMResponse(
        stop_reason=_stop_reason(raw.stop_reason),
        content=content,
        usage=Usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        ),
        # The model that actually served the request, which is not necessarily the one
        # we asked for once server-side fallbacks are in play.
        model=raw.model,
        stop_details=stop_details,
    )


_KNOWN_STOP_REASONS: frozenset[str] = frozenset(
    {"end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal"}
)


def _stop_reason(value: str | None) -> StopReason:
    """
    Map the provider's stop reason onto ours, refusing anything unrecognised.

    Unknown block *types* are carried through opaquely, but an unknown stop *reason* is
    the opposite case: it is precisely the field the loop branches on, so guessing would
    mean silently treating a new terminal condition as an ordinary answer. Failing here
    is the safe direction.
    """
    if value in _KNOWN_STOP_REASONS:
        return value  # type: ignore[return-value]
    raise LLMRequestError(
        f"Unrecognised stop_reason {value!r}. The loop branches on this field, so it "
        f"must not be guessed at — add it to the StopReason literal deliberately."
    )
