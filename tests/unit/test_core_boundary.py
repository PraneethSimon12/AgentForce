"""
The architectural test: `core/` may not import `adapters/`.

CLAUDE.md §5 states this boundary, and a rule that lives only in a document is a rule
that erodes — someone (me, at 1am, needing a database session in the loop) writes the
import, it works, and the erosion is invisible until the unit suite needs Docker.

This test makes the boundary executable. It parses every module under `app/core/` and
fails on the first import that crosses the line. It is the reason the whole suite can
run with no network, no database and no model weights.
"""

from __future__ import annotations

import ast
from pathlib import Path

CORE = Path(__file__).resolve().parents[2] / "app" / "core"

# Everything core is permitted to reach for. `pydantic` earns its place because the tool
# registry's entire design is Pydantic generating JSON Schema; `app.core` is itself.
# Anything else — SQLAlchemy, redis, anthropic, celery, httpx — is IO, and IO is the
# adapter's job.
ALLOWED_THIRD_PARTY = {"pydantic"}
STDLIB_IS_FINE = True  # stdlib imports are not checked; none of them perform IO for us


def _imported_roots(module: Path) -> set[str]:
    """Return the top-level package name of every import in one module."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module)
    return roots


def test_core_never_imports_an_adapter() -> None:
    offenders: list[str] = []
    for module in CORE.rglob("*.py"):
        for imported in _imported_roots(module):
            if imported.startswith(("app.adapters", "app.api", "app.workers")):
                offenders.append(f"{module.relative_to(CORE.parent.parent)} -> {imported}")

    assert not offenders, (
        "core/ must not import the IO layers; this is what keeps the unit suite "
        "infrastructure-free:\n  " + "\n  ".join(offenders)
    )


def test_core_never_imports_a_client_library() -> None:
    """
    No SQLAlchemy, no redis, no anthropic, no celery inside `core/`.

    Stronger than the previous test and the one that actually bites: importing the
    Anthropic SDK directly in the loop would satisfy "no adapter import" while destroying
    the property the boundary exists for.
    """
    banned = {"sqlalchemy", "redis", "anthropic", "celery", "httpx", "fastapi", "asyncpg"}
    offenders: list[str] = []
    for module in CORE.rglob("*.py"):
        for imported in _imported_roots(module):
            if imported.split(".")[0] in banned:
                offenders.append(f"{module.relative_to(CORE.parent.parent)} -> {imported}")

    assert not offenders, "core/ must stay free of IO libraries:\n  " + "\n  ".join(offenders)


def test_settings_is_not_imported_by_core() -> None:
    """
    Core takes its configuration as arguments; it never reads the environment.

    A `core` module importing `app.settings` would reintroduce a global dependency and
    make the pure logic untestable without an environment — the same problem the ports
    exist to solve, arriving through a different door.
    """
    offenders = [
        str(module.relative_to(CORE.parent.parent))
        for module in CORE.rglob("*.py")
        if "app.settings" in _imported_roots(module)
    ]

    assert not offenders, f"core/ must not read configuration: {offenders}"
