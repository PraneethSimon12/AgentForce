"""
The exception types `core/` raises and catches.

Only the ones something already raises or handles live here. The point of this module is
one distinction, made once: **retryable versus not.** CLAUDE.md §4 bans catching a single
broad exception class from the SDK precisely because that distinction is the entire
reason for catching at all — a 429 should back off and try again, a 400 never will
succeed and retrying it just spends money slowly.

The adapter is what maps provider exceptions onto these. That is what lets the loop
implement a retry policy (D-009) without importing `anthropic` and without knowing that
`RateLimitError` exists.
"""

from __future__ import annotations


class AgentForgeError(Exception):
    """Base for every error this system raises deliberately."""


class LLMError(AgentForgeError):
    """A call to the model provider failed."""


class LLMTransportError(LLMError):
    """
    A provider failure that may succeed if tried again.

    Connection failures, timeouts, 429s, 5xx. The loop's bounded retry applies to these
    and only these.
    """


class LLMRequestError(LLMError):
    """
    A provider failure that will never succeed if tried again.

    A malformed request, an unknown model, a bad API key, a request over the context
    limit. Retrying is not just useless, it is expensive and it hides the real bug, so
    the loop fails the run and records why.
    """
