"""
A scripted LLM. The keystone of the v0 test strategy.

Scripted, never random (CLAUDE.md §4). It replays a fixed list of outcomes in order, so
a test can say "the model will ask for the calculator, then answer" and assert the
entire resulting conversation byte for byte. Randomness would make the loop's tests
probabilistic, which for a component whose job is *exactly-once tool execution* is worse
than no tests at all.

Three properties earn their place:

1. **It records what it was sent.** Most of the subtle bugs in an agent loop are in the
   request, not the response — results split across two user messages, a missing
   `tool_use_id`, a thinking block that did not round-trip. Those are only assertable if
   the fake keeps the requests.
2. **It can be scripted to raise.** A script entry may be an exception, which is what
   makes v1's retry and resume paths testable without a flaky network.
3. **It runs out loudly.** A loop that makes more calls than the script has answers is a
   loop that is not terminating, and that is exactly the bug worth failing on.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.core.runtime.messages import (
    ContentBlock,
    Effort,
    LLMResponse,
    Message,
    StopDetails,
    StopReason,
    TextBlock,
    ToolSchema,
    ToolUseBlock,
    Usage,
)

DEFAULT_MODEL = "fake-model-1"


class ScriptExhausted(AssertionError):
    """
    The loop asked for more responses than the script provides.

    An AssertionError rather than one of our own error types: this is never a production
    condition, it is a test whose expectations and whose subject disagree. Raising the
    type pytest already treats as a failed assertion keeps that framing.
    """


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """Exactly what the loop sent, kept so tests can assert on the request."""

    messages: tuple[Message, ...]
    system: str
    tools: tuple[ToolSchema, ...]
    max_tokens: int
    effort: Effort


@dataclass
class FakeLLM:
    """
    An `LLMClient` that replays a script.

    Structural conformance to the port — no inheritance — which is the same property
    `SystemClock` has and the reason `core/` never learns either class exists.
    """

    script: Sequence[LLMResponse | Exception]
    calls: list[RecordedCall] = field(default_factory=list)

    async def complete(
        self,
        *,
        messages: Sequence[Message],
        system: str,
        tools: Sequence[ToolSchema],
        max_tokens: int,
        effort: Effort,
    ) -> LLMResponse:
        """Return the next scripted outcome, recording the request first."""
        self.calls.append(
            RecordedCall(
                messages=tuple(messages),
                system=system,
                tools=tuple(tools),
                max_tokens=max_tokens,
                effort=effort,
            )
        )
        if len(self.calls) > len(self.script):
            raise ScriptExhausted(
                f"The loop made {len(self.calls)} calls but the script has "
                f"{len(self.script)}. Either the loop is not terminating, or the "
                f"script is missing a response."
            )
        outcome = self.script[len(self.calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# --- Script builders -----------------------------------------------------------------
#
# Tests read far better as `says("4")` than as a six-line LLMResponse literal, and a
# test that is hard to read is a test that gets written wrong.


def says(text: str, *, input_tokens: int = 100, output_tokens: int = 10) -> LLMResponse:
    """A final answer: the model is done."""
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        model=DEFAULT_MODEL,
    )


def calls_tool(
    name: str,
    tool_input: dict[str, object],
    *,
    tool_use_id: str = "toolu_fake_1",
    thinking: str | None = None,
    input_tokens: int = 100,
    output_tokens: int = 20,
) -> LLMResponse:
    """
    A turn that asks for one tool.

    `thinking` adds an opaque thinking block ahead of the tool call, which is what a
    real turn looks like and what the round-trip assertions need.
    """
    content: list[ContentBlock] = []
    if thinking is not None:
        content.append(_opaque({"type": "thinking", "thinking": thinking, "signature": "sig-fake"}))
    content.append(ToolUseBlock(id=tool_use_id, name=name, input=dict(tool_input)))
    return LLMResponse(
        stop_reason="tool_use",
        content=content,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        model=DEFAULT_MODEL,
    )


def calls_tools(
    *requested: tuple[str, dict[str, object], str],
    input_tokens: int = 100,
    output_tokens: int = 40,
) -> LLMResponse:
    """
    A turn that asks for several tools at once, as `(name, input, tool_use_id)` triples.

    Parallel tool use is the default behaviour of the API, and the reason it needs its
    own builder is the trap it guards: all the results must come back in a *single* user
    message, and nothing errors if they do not.
    """
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(id=tool_use_id, name=name, input=dict(tool_input))
            for name, tool_input, tool_use_id in requested
        ],
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        model=DEFAULT_MODEL,
    )


def refuses(category: str = "cyber") -> LLMResponse:
    """
    A refusal, which arrives as a successful HTTP 200 and not as an exception.

    This is the response that breaks loops written by people who read `content` before
    they read `stop_reason`.
    """
    return LLMResponse(
        stop_reason="refusal",
        content=[],
        usage=Usage(input_tokens=100, output_tokens=0),
        model=DEFAULT_MODEL,
        stop_details=StopDetails(type="refusal", category=category),
    )


def truncated(partial_tool_call: bool = False) -> LLMResponse:
    """
    A turn cut off at `max_tokens`.

    With `partial_tool_call`, the content ends in a tool_use block that the model never
    finished describing — syntactically valid, semantically broken. Acting on it is the
    bug; the loop has to notice `stop_reason` first.
    """
    content: list[ContentBlock] = [TextBlock(text="Let me work that out")]
    if partial_tool_call:
        content.append(ToolUseBlock(id="toolu_partial", name="calculator", input={}))
    return LLMResponse(
        stop_reason="max_tokens",
        content=content,
        usage=Usage(input_tokens=100, output_tokens=16_000),
        model=DEFAULT_MODEL,
    )


def responds(
    *,
    stop_reason: StopReason,
    content: Sequence[ContentBlock],
    input_tokens: int = 100,
    output_tokens: int = 10,
) -> LLMResponse:
    """Escape hatch for a shape the named builders do not cover."""
    return LLMResponse(
        stop_reason=stop_reason,
        content=list(content),
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        model=DEFAULT_MODEL,
    )


def _opaque(raw: dict[str, object]) -> ContentBlock:
    from app.core.runtime.messages import OpaqueBlock

    return OpaqueBlock.model_validate(raw)
