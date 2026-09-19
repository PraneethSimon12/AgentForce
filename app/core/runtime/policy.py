"""
The cost guardrails, as code rather than intentions.

CLAUDE.md §7 puts this in v0 on purpose, before the loop is durable and before it ever
meets a real API key. A runaway loop is the most expensive bug available to this project
— a single mistake can burn a month's budget in an afternoon — and the only protection
that works is one the loop structurally cannot exceed.

Two limits, checked before every step: a hard step cap and a hard token budget.
"""

from __future__ import annotations

from app.core.runtime.errors import AgentForgeError
from app.core.runtime.state import RunLimits, RunUsage

# Below this many tokens of headroom, a request is not worth making. The reason is not
# thrift: a request issued with a tiny `max_tokens` gets truncated, and the dangerous
# truncation is mid-tool-call, which produces a syntactically valid response with a
# broken intent (CLAUDE.md §8). Stopping cleanly beats spending the last 200 tokens on
# an answer that cannot be trusted.
MIN_VIABLE_OUTPUT_TOKENS = 1024


class BudgetExceeded(AgentForgeError):
    """
    A run hit its step cap or its token budget.

    Carries the numbers because they go straight into the wire error (plan.md §2.1) and
    because "which limit, at what value, observed when" is the whole content of the
    message. Note this is a *run outcome*, not an HTTP failure — the request that
    created the run already succeeded.
    """

    def __init__(self, limit_name: str, limit: int, observed: int, step_idx: int) -> None:
        super().__init__(
            f"Run exceeded its {limit_name} of {limit} at step {step_idx} (observed {observed})."
        )
        self.limit_name = limit_name
        self.limit = limit
        self.observed = observed
        self.step_idx = step_idx


class LoopPolicy:
    """
    Decides whether the loop may take another step, and how large that step may be.

    Responsibility: limits only. Does NOT execute anything, does NOT know what a tool
    is, and does NOT decide *what* the next step does — that is the model's job and the
    loop's job respectively. Separated from the loop so the expensive-mistake logic can
    be tested exhaustively without a model, a tool or a conversation.
    """

    def __init__(self, limits: RunLimits) -> None:
        self._limits = limits

    @property
    def limits(self) -> RunLimits:
        return self._limits

    def check(self, usage: RunUsage) -> None:
        """
        Authorise one more step, or refuse.

        Called *before* the request, never after. Checking afterwards would mean the
        budget is discovered to be blown by the very call that blew it, which is the
        one thing a spend limit exists to prevent.

        Raises: BudgetExceeded if the step cap is reached, if the token budget is
            reached, or if the remaining headroom is too small to produce a usable
            response.
        """
        if usage.steps >= self._limits.max_steps:
            raise BudgetExceeded("step cap", self._limits.max_steps, usage.steps, usage.steps)

        remaining = self._limits.token_budget - usage.total_tokens
        if remaining < MIN_VIABLE_OUTPUT_TOKENS:
            raise BudgetExceeded(
                "token budget",
                self._limits.token_budget,
                usage.total_tokens,
                usage.steps,
            )

    def max_tokens_for_next_step(self, usage: RunUsage) -> int:
        """
        The `max_tokens` to send, clamped to what the budget can still afford.

        Preconditions: `check(usage)` has passed, so the remaining headroom is at least
        MIN_VIABLE_OUTPUT_TOKENS.

        This is what bounds the overshoot. A step's cost cannot be known before making
        it, so a pre-flight check alone allows the budget to be exceeded by up to one
        step. Clamping the output ceiling to the remaining headroom caps that overshoot
        at the input tokens of a single request rather than at an arbitrary amount.
        """
        remaining = self._limits.token_budget - usage.total_tokens
        return min(self._limits.max_output_tokens, remaining)
