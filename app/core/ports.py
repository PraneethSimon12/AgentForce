"""
The seams: every Protocol `core/` depends on, and no implementation of any of them.

`core/` contains no IO and imports nothing from `adapters/`. These Protocols are the
mechanism. The agent loop asks for something shaped like `LLMClient`; in production it
gets the Anthropic adapter, and in a unit test it gets `FakeLLM` replaying a scripted
list of responses. Neither the loop nor the test knows which. That is Dependency
Inversion, and the payoff is concrete: the v0 exit criterion is a green test suite with
no network, no Docker and no model weights, which is only reachable if nothing in
`core/` can reach the outside world.

`Protocol`, not `ABC`, and the difference matters. An ABC demands that implementations
inherit from it, which means `adapters/` would import `core/` *and* `core/` would define
the base class the adapter is built on — a two-way coupling. A Protocol is structural:
any class with the right methods satisfies it, checked statically by mypy, with no
inheritance and no import in that direction at all.

Not `@runtime_checkable`: that only verifies method *names* exist at runtime, never their
signatures, so it buys a false sense of safety in exchange for an `isinstance` check we
do not need. mypy --strict already checks the real thing, before the code runs.

Ports arrive with the phase that implements them. `RunStore` and `EventBus` are not here
yet because v1 and v2 have not defined what they store and publish — see D-014.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from app.core.runtime.messages import Effort, LLMResponse, Message, ToolSchema


class Clock(Protocol):
    """
    Everything time-dependent, in one injectable place.

    A port because time is IO in every way that matters for testing. Per-step timeouts,
    retry backoff and (in v1) lease expiry are all decisions made against a clock, and a
    test that has to wait 60 real seconds to prove a 60-second timeout fires is a test
    nobody runs. A fake clock makes those paths instant and deterministic.
    """

    def now(self) -> datetime:
        """Return the current time as an aware UTC datetime. Never naive."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Yield control for `seconds`. Must not block the event loop."""
        ...


class LLMClient(Protocol):
    """
    One request to a language model. The narrowest useful surface.

    Deliberately *not* on this interface:

    - **The model id.** An implementation is bound to one model at construction. The
      eval harness wanting a cheap judge alongside the expensive agent builds two
      clients, which is honest — a client is a configured thing, not a dispatcher.
    - **Retry.** The port raises a retryable or a non-retryable error and stops there.
      The loop owns the retry budget, because the budget is per *step* and per *run*,
      facts this object cannot see (D-009).
    - **Prompt caching.** `system` is a plain string; the adapter is what wraps it with
      `cache_control` before it goes on the wire. Caching is a property of the transport,
      not of the conversation, and core has no business knowing about a cache breakpoint.
    - **Streaming.** Arrives in v2 as a second method, which is an additive change. A
      Protocol can grow; the implementations simply have to keep up.
    """

    async def complete(
        self,
        *,
        messages: Sequence[Message],
        system: str,
        tools: Sequence[ToolSchema],
        max_tokens: int,
        effort: Effort,
    ) -> LLMResponse:
        """
        Send one request and return the translated response.

        Responsibility: exactly one round trip. Does NOT execute tools (the loop does),
        does NOT append to the conversation (the loop does), and does NOT decide whether
        to continue (the policy does).

        Preconditions: `messages` alternates sensibly and ends with a user turn; every
        `tool_result` block in it carries a `tool_use_id` matching an earlier
        `tool_use`. `tools` must be ordered deterministically — the tool list is part of
        the cache prefix, so reordering it silently costs a cache hit on every step.

        Postcondition: the returned `stop_reason` has been read from the provider, never
        inferred from the content. A response with no text is a legitimate outcome, not
        an error.

        Raises:
            LLMTransportError: the call failed in a way that may succeed on retry.
            LLMRequestError: the call failed in a way that will not.
        """
        ...
