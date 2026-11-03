"""Purge waits for active writers without serializing normal writes or deadlocking nesting."""

import asyncio

import pytest
from coppermind_store.maintenance import MaintenanceGate


async def test_exclusive_barrier_preserves_overlapping_and_nested_writers():
    gate = MaintenanceGate()
    entered = asyncio.Event()
    release = asyncio.Event()
    purging = asyncio.Event()
    finish_purge = asyncio.Event()
    later_entered = asyncio.Event()

    async def active_writer():
        async with gate.shared():
            entered.set()
            await release.wait()
            # A writer already running can finish a nested store operation.
            async with gate.shared():
                pass

    async def purge():
        async with gate.exclusive():
            purging.set()
            await finish_purge.wait()

    async def later_writer():
        async with gate.shared():
            later_entered.set()

    first = asyncio.create_task(active_writer())
    await entered.wait()
    async with gate.shared():
        pass  # Ordinary writers overlap.
    exclusive = asyncio.create_task(purge())
    await asyncio.sleep(0)
    last = asyncio.create_task(later_writer())
    await asyncio.sleep(0)
    assert not purging.is_set() and not later_entered.is_set()
    release.set()
    await asyncio.wait_for(purging.wait(), 1)
    assert not later_entered.is_set()
    finish_purge.set()
    await asyncio.wait_for(asyncio.gather(first, exclusive, last), 1)
    assert later_entered.is_set()


async def test_cancelled_purge_does_not_block_writers():
    gate = MaintenanceGate()
    async with gate.shared():

        async def wait_for_purge():
            async with gate.exclusive():
                pass

        pending = asyncio.create_task(wait_for_purge())
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    async with gate.shared():
        assert gate.waiting == 0
