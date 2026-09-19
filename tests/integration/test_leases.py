"""
Integration tests for run leases. Real Postgres, real concurrency.

A lease answers one question: **which worker owns this run right now?** Getting it wrong
does not produce an error — it produces two workers writing steps for the same run, each
believing it is alone, and the damage shows up later as a conversation with duplicated
turns and a doubled bill.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.adapters.db.run_store import PostgresRunStore
from app.core.runtime.state import NewRun, RunLimits, RunStatus

pytestmark = pytest.mark.integration


def new_run(**overrides: object) -> NewRun:
    spec: dict[str, object] = {
        "agent": "researcher",
        "input": {"query": "q"},
        "limits": RunLimits(),
        "effort": "medium",
        "prompt_name": "agent",
        "prompt_version": 1,
        "model": "claude-opus-5",
    }
    spec.update(overrides)
    return NewRun(**spec)  # type: ignore[arg-type]


async def expire_lease(engine: AsyncEngine, run_id: object) -> None:
    """
    Push a lease's expiry into the past, simulating a worker that died.

    Done in SQL against `now()` rather than by sleeping, because the expiry comparison
    uses the *database's* clock and a test that waits for wall-clock time to pass is a
    test that is slow and flaky at once.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE runs SET lease_expires_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": run_id},
        )


async def test_claiming_a_free_run_succeeds_and_marks_it_running(
    store: PostgresRunStore,
) -> None:
    run = await store.create(new_run())

    claimed = await store.claim(run.id, owner="worker-1", ttl_seconds=30)

    assert claimed is not None
    assert claimed.status is RunStatus.RUNNING
    assert claimed.lease_owner == "worker-1"


async def test_a_second_worker_cannot_claim_a_live_lease(store: PostgresRunStore) -> None:
    """
    The core guarantee. Not an error — just None, because a recovery scan finding a run
    already in progress is ordinary.
    """
    run = await store.create(new_run())
    await store.claim(run.id, owner="worker-1", ttl_seconds=30)

    assert await store.claim(run.id, owner="worker-2", ttl_seconds=30) is None


async def test_concurrent_claims_produce_exactly_one_winner(
    store: PostgresRunStore,
) -> None:
    """
    Twenty workers, one run, one winner.

    This is the test that a check-then-act implementation fails. A SELECT that finds the
    lease free followed by an UPDATE that takes it has a window between the two, and
    under real concurrency several workers pass through it together. A single
    conditional UPDATE has no such window — the row lock is taken by the UPDATE itself.
    """
    run = await store.create(new_run())

    results = await asyncio.gather(
        *(store.claim(run.id, owner=f"worker-{i}", ttl_seconds=30) for i in range(20))
    )

    winners = [r for r in results if r is not None]
    assert len(winners) == 1


async def test_an_expired_lease_can_be_taken_over(
    store: PostgresRunStore, engine: AsyncEngine
) -> None:
    """A crashed worker's run must become available without anyone intervening."""
    run = await store.create(new_run())
    await store.claim(run.id, owner="worker-1", ttl_seconds=30)
    await expire_lease(engine, run.id)

    taken = await store.claim(run.id, owner="worker-2", ttl_seconds=30)

    assert taken is not None
    assert taken.lease_owner == "worker-2"


async def test_renewing_keeps_the_lease_alive(store: PostgresRunStore) -> None:
    run = await store.create(new_run())
    await store.claim(run.id, owner="worker-1", ttl_seconds=30)

    assert await store.renew(run.id, owner="worker-1", ttl_seconds=60) is True


async def test_a_worker_that_lost_its_lease_cannot_renew(
    store: PostgresRunStore, engine: AsyncEngine
) -> None:
    """
    The half that matters most.

    A worker paused long enough for its lease to expire has *already* had the run taken
    from it. It must learn that here and stop, rather than carry on writing steps
    alongside the new owner — which nothing else would catch.
    """
    run = await store.create(new_run())
    await store.claim(run.id, owner="worker-1", ttl_seconds=30)
    await expire_lease(engine, run.id)
    await store.claim(run.id, owner="worker-2", ttl_seconds=30)

    assert await store.renew(run.id, owner="worker-1", ttl_seconds=30) is False


async def test_releasing_leaves_the_run_resumable(store: PostgresRunStore) -> None:
    """
    The clean counterpart to a crash.

    A worker shutting down releases, so the run is picked up immediately instead of
    after the TTL expires — the difference between a rolling deploy costing nothing and
    costing one TTL per run in flight.
    """
    run = await store.create(new_run())
    await store.claim(run.id, owner="worker-1", ttl_seconds=30)

    await store.release(run.id, owner="worker-1")

    reloaded = await store.load(run.id)
    assert reloaded.status is RunStatus.PAUSED
    assert reloaded.lease_owner is None
    assert await store.claim(run.id, owner="worker-2", ttl_seconds=30) is not None


async def test_a_terminal_run_cannot_be_claimed(store: PostgresRunStore) -> None:
    """Nothing left to do, so the recovery scan must not keep finding it."""
    from app.core.runtime.state import RunOutcome, RunUsage

    run = await store.create(new_run())
    await store.finish(
        run.id,
        RunOutcome(status=RunStatus.COMPLETED, usage=RunUsage(), messages=(), answer="done"),
    )

    assert await store.claim(run.id, owner="worker-1", ttl_seconds=30) is None


# --- The recovery scan ---------------------------------------------------------------


async def test_the_scan_picks_up_an_abandoned_run(
    store: PostgresRunStore, engine: AsyncEngine
) -> None:
    run = await store.create(new_run())
    await store.claim(run.id, owner="dead-worker", ttl_seconds=30)
    await expire_lease(engine, run.id)

    reclaimed = await store.claim_next_reclaimable(owner="worker-2", ttl_seconds=30)

    assert reclaimed is not None
    assert reclaimed.id == run.id
    assert reclaimed.lease_owner == "worker-2"


async def test_the_scan_gives_each_worker_a_different_run(
    store: PostgresRunStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """
    `FOR UPDATE SKIP LOCKED` earning its place.

    Without SKIP LOCKED, every worker polling would block on the same first row and the
    pool would serialise — ten workers doing one worker's throughput. With it, each
    skips what another has locked and takes the next, so three concurrent scans over
    three queued runs claim three distinct runs.
    """
    runs = [await store.create(new_run()) for _ in range(3)]

    workers = [PostgresRunStore(session_factory) for _ in range(3)]
    claimed = await asyncio.gather(
        *(
            w.claim_next_reclaimable(owner=f"worker-{i}", ttl_seconds=30)
            for i, w in enumerate(workers)
        )
    )

    ids = {c.id for c in claimed if c is not None}
    assert len(ids) == 3
    assert ids == {r.id for r in runs}


async def test_the_scan_returns_none_when_everything_is_busy(
    store: PostgresRunStore,
) -> None:
    run = await store.create(new_run())
    await store.claim(run.id, owner="worker-1", ttl_seconds=30)

    assert await store.claim_next_reclaimable(owner="worker-2", ttl_seconds=30) is None
