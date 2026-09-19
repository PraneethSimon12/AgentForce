"""
Backoff, hand-rolled.

`tenacity` would do this, and the header of `pyproject.toml` records that we refused it
deliberately (CLAUDE.md Rule 5). It is about forty lines, the semantics matter to us,
and the interesting part — how many layers of retry are actually in play — is something
a library encourages you to stop thinking about.

There are **three** layers here and knowing that is the point (D-009):

1. The Anthropic SDK retries connection errors, 408, 409, 429 and 5xx twice by default.
   We leave that at its default and count it.
2. The loop retries a failed *step*, bounded per step.
3. The run has a total retry budget, stored on the row so a crash does not hand a
   resumed run a fresh allowance.

Multiply them without noticing and one user action becomes 2 x 3 x 3 = 18 attempts,
which is how a rate limit becomes an outage.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """
    Exponential backoff with full jitter.

    Pure and deterministic: the random draw is an *argument*, not something this class
    reaches for. That is what lets the delay schedule be unit-tested exactly rather than
    approximately, and it keeps the only source of nondeterminism in the caller where it
    is visible.
    """

    base_seconds: float = 0.5
    max_seconds: float = 30.0

    def delay_for(self, attempt: int, random_value: float) -> float:
        """
        Seconds to wait before `attempt` (1-based: attempt 2 is the first retry).

        **Full jitter**, not a fixed exponential. If a hundred runs hit the same rate
        limit at the same moment and all back off exactly 0.5s, they retry in lockstep
        and re-trigger the limit together — the delay does nothing except move the
        stampede. Spreading each retry uniformly across its whole window is what
        actually decorrelates them.

        Preconditions: `attempt >= 1`, `0.0 <= random_value < 1.0`.
        """
        if attempt <= 1:
            return 0.0
        ceiling: float = min(self.max_seconds, self.base_seconds * 2.0 ** (attempt - 2))
        return ceiling * random_value
