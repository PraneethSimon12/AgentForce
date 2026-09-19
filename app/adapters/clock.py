"""
The real clock. The first implementation of a port, and the smallest possible proof
that the port is implementable at all.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.core.ports import Clock


class SystemClock:
    """
    Wall-clock time and real sleeping.

    Note that it does not inherit from `Clock`. Nothing requires it to: `Clock` is a
    Protocol, so conformance is structural and checked by mypy. The absence of that
    base class is the whole point of D-014's argument about the direction of the
    dependency — `adapters/` imports `core/`, never the reverse, and `core/` does not
    need to know this class exists.
    """

    def now(self) -> datetime:
        """
        Return an aware UTC datetime. Never naive.

        `datetime.now()` without a timezone returns a naive local time, which compares
        and serialises incorrectly and is the single most common way timestamps go
        wrong. Passing UTC explicitly makes that impossible rather than merely unlikely.
        """
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        """Yield to the event loop. Never `time.sleep`, which would block every request."""
        await asyncio.sleep(seconds)


if TYPE_CHECKING:
    # A compile-time assertion that SystemClock satisfies Clock. If a method is renamed
    # or its signature drifts, mypy fails here rather than at the first call site — which
    # is the entire value of using a Protocol instead of duck typing and hoping.
    _conforms: Clock = SystemClock()
