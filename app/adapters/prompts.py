"""
Loading versioned prompt files.

CLAUDE.md §4: prompts live in `app/prompts/` as versioned text files, never as string
literals in business logic. A prompt changes system behaviour exactly the way an
algorithm does, so it has to be diffable in review, attributable in git blame, and
recorded next to the eval numbers it produced. Without the version on the result, a
quality regression cannot be traced to the edit that caused it, and the eval harness
stops being a regression gate (D-011).

This lives in `adapters/` because reading a file is IO, and `core/` receives the loaded
string as an argument.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from app.core.runtime.errors import AgentForgeError

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


class PromptNotFound(AgentForgeError):
    """A prompt file that does not exist. Raised at startup, not mid-run."""


@cache
def load_prompt(name: str, version: int) -> str:
    """
    Load `app/prompts/{name}.v{version}.txt`.

    Cached for two reasons. The obvious one is that it is read on every run and never
    changes while the process lives. The load-bearing one is that the system prompt is
    the prompt-cache prefix: returning the identical string object every time removes
    any chance of a per-call transformation creeping in and silently invalidating it.

    Line endings are normalised to `\\n`. This is not cosmetic — git checks text files
    out with CRLF on Windows and LF in the Linux container, so the same committed file
    would otherwise produce two different byte sequences, and therefore two different
    cache prefixes, between a developer's machine and production. The symptom would be
    `cache_read_input_tokens` sitting at zero in one environment and nowhere else, which
    is an unpleasant afternoon.

    Raises: PromptNotFound.
    """
    path = PROMPTS_DIR / f"{name}.v{version}.txt"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        available = sorted(p.name for p in PROMPTS_DIR.glob("*.txt"))
        raise PromptNotFound(
            f"No prompt at {path.name}. Available: {', '.join(available) or '(none)'}"
        ) from None
    return raw.replace("\r\n", "\n")
