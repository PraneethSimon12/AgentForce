"""
The conversation vocabulary: what a message is, what comes back from the model.

These are AgentForge's own types, not the Anthropic SDK's. `core/` never imports
`anthropic`, so the agent loop can be unit-tested against `FakeLLM` with the SDK not
installed, and so swapping providers is an adapter change rather than a rewrite of the
loop. The cost is a translation layer in `adapters/llm/` — see D-014 for why that is
worth paying, and for the trick that makes it safe.

**The trick: we only give a type to the blocks the loop actually interprets.**

The loop reads exactly three kinds of block — text (the answer), tool_use (what to
dispatch), tool_result (what to send back). Everything else the API emits, today or in
two years — thinking, redacted_thinking, server_tool_use, compaction, fallback — is an
`OpaqueBlock` that we carry through untouched.

That is not laziness, it is the fix for a specific trap (CLAUDE.md §8): thinking blocks
must be echoed back **unchanged** when continuing a turn, and they must round-trip
byte-identically out of Postgres or the prompt cache silently stops matching. A block we
never parse into fields is a block we cannot mangle on the way back out. It also means a
new block type in a future API version cannot break replay — the same
ignore-what-you-do-not-understand rule that plan.md §2.3 already applies to SSE events.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Role = Literal["user", "assistant"]

# Controls thinking depth and therefore token spend (CLAUDE.md §7). Lives here rather
# than in settings.py because it is part of the request vocabulary; settings imports it.
Effort = Literal["low", "medium", "high", "xhigh", "max"]

# Every value the API can return. `refusal` is the one that catches people out: it
# arrives as an HTTP 200, so nothing raises — the loop has to check `stop_reason` before
# it reads `content`, or it dies later with a confusing error from an empty block list.
StopReason = Literal[
    "end_turn",
    "max_tokens",
    "stop_sequence",
    "tool_use",
    "pause_turn",
    "refusal",
]


class TextBlock(BaseModel):
    """Prose from the model. Interpreted: this is where the final answer comes from."""

    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    """
    A request from the model to run a tool. Interpreted: the loop dispatches on this.

    `input` stays `dict[str, Any]` — the raw, already-parsed JSON object. It is validated
    against the tool's Pydantic model by the registry (v0.3), not here, because this type
    has no idea which tools exist. Note that it is parsed JSON and never a string: tool
    argument escaping varies between models, so string-matching the serialised form is a
    bug waiting for a payload with a quote in it (CLAUDE.md §8).
    """

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


class ToolResultBlock(BaseModel):
    """
    What we send back after running a tool. Interpreted: the loop constructs these.

    `tool_use_id` must match the `id` of the `ToolUseBlock` that asked for it, and a tool
    that *failed* still gets a block, with `is_error=True`. Dropping the result of a
    failed tool leaves the conversation malformed — the model asked a question it never
    got an answer to (CLAUDE.md §8).
    """

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


class OpaqueBlock(BaseModel):
    """
    Any block the loop does not interpret, preserved verbatim for the round trip.

    Thinking blocks are the important case: the loop never reads one, but it must hand
    it back unchanged on the next request. `extra="allow"` keeps every field the API
    sent — including `signature`, whose absence invalidates the turn — so `model_dump()`
    reproduces the original object rather than our idea of it.
    """

    model_config = ConfigDict(extra="allow")

    type: str


# Parsing a raw block dict into one of these is the *adapter's* job — it constructs the
# right class explicitly. Core never parses provider JSON, so this union needs no
# discriminator yet. When v1 replays messages out of Postgres, that reader gets one.
ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock | OpaqueBlock


class Message(BaseModel):
    """
    One turn of the conversation, as it will be replayed to the model.

    All `tool_result` blocks answering a single assistant turn belong in **one** user
    message. Splitting them across several silently teaches the model to stop making
    parallel tool calls — nothing errors, the behaviour just quietly degrades, which is
    why the type takes a list of blocks and the loop must not be tempted to append one
    message per result (CLAUDE.md §8).
    """

    role: Role
    content: list[ContentBlock]


class Usage(BaseModel):
    """
    Token accounting for one request. Drives the run budget and the cost dashboard.

    `cache_read_input_tokens` is here because it is the prompt-cache health signal: the
    system prompt and tool schemas are byte-identical across every step of every run, so
    they are the ideal cache prefix. If this stays zero across repeated runs, something
    is invalidating that prefix — and that shows up on the bill long before it shows up
    anywhere else (CLAUDE.md §7).
    """

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class StopDetails(BaseModel):
    """Structured reason for a refusal. Populated only when `stop_reason == "refusal"`."""

    type: str
    category: str | None = None
    explanation: str | None = None


class LLMResponse(BaseModel):
    """
    One model response, translated out of the provider's shape.

    `model` is the model that actually served the request, which is not necessarily the
    one we asked for, and belongs in the run record for exactly that reason.
    """

    stop_reason: StopReason
    content: list[ContentBlock]
    usage: Usage
    model: str
    # Null for every stop reason except `refusal` — guard before reading it.
    stop_details: StopDetails | None = None


class ToolSchema(BaseModel):
    """
    A tool as the model sees it: the payload of one entry in the `tools` parameter.

    `input_schema` is JSON Schema, produced by `model_json_schema()` on the tool's
    Pydantic input model (v0.3). One definition generates both the schema the model
    plans against and the validator that checks what it sends back, so the two cannot
    drift apart. That is the load-bearing idea of the registry.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
