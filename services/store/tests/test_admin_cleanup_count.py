"""Pages only read a durable count maintained by the background worker."""

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import pytest
from coppermind_store import admin_storage
from coppermind_store.control import ControlState
from coppermind_store.notes import LocalStore

from coppermind.settings import Wiring
from coppermind.store_protocol import NotesFilesystemUnavailable


@pytest.fixture
def storage(tmp_path: Path):
    wiring = Wiring(data_dir=tmp_path)
    control = ControlState(wiring.state_dir)
    control.ensure_defaults()
    store = LocalStore(wiring.notes_dir, control, cast(Any, None), wiring.sources_dir)
    return store.admin.storage


async def test_requests_only_read_and_count_survives_restart(storage, monkeypatch):
    calls = 0

    async def candidates(*args):
        nonlocal calls
        calls += 1
        return [{}, {}], {}

    monkeypatch.setattr(admin_storage.purge, "candidates", candidates)
    counts = await asyncio.gather(*(storage.cleanup_count() for _ in range(20)))
    assert counts == [{"count": None, "computed_at": None}] * 20
    assert calls == 0
    await storage.refresh_cleanup_count()
    assert calls == 1
    known = await storage.cleanup_count()
    assert known["count"] == 2
    assert known["computed_at"]
    # A new store and ControlState read the durable value without computing it.
    previous = storage.store
    restarted = LocalStore(
        previous.notes_root,
        ControlState(previous.control.store.state_dir),
        cast(Any, None),
        previous.sources_root,
    )
    assert await restarted.admin.storage.cleanup_count() == known
    assert calls == 1


async def test_worker_refreshes_at_startup_periodically_and_when_woken(storage, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    published = asyncio.Event()
    calls = 0
    intervals = []
    real_wait_for = asyncio.wait_for

    async def candidates(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
        return [{}] * calls, {}

    async def wait_for(awaitable, timeout):
        intervals.append(timeout)
        published.set()
        # Advance one periodic deadline without sleeping five minutes.
        if len(intervals) == 1:
            return await real_wait_for(awaitable, timeout=0.01)
        return await real_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(admin_storage.purge, "candidates", candidates)
    monkeypatch.setattr(admin_storage.asyncio, "wait_for", wait_for)
    worker = asyncio.create_task(storage.run_cleanup_count())
    try:
        async with asyncio.timeout(2):
            await started.wait()
            counts = await asyncio.gather(*(storage.cleanup_count() for _ in range(20)))
            assert all(count["count"] is None for count in counts)
            assert calls == 1
            release.set()
            while len(intervals) < 2:
                published.clear()
                await published.wait()
            assert (await storage.cleanup_count())["count"] == 2
            assert intervals == [300, 300]
            published.clear()
            await storage.set_count_interval({"seconds": 900})
            await published.wait()
            assert calls == 3
            assert intervals[-1] == 900
            assert (await storage.cleanup_count())["count"] == 3
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


async def test_failed_refresh_retains_count_and_timestamp(storage, monkeypatch):
    async def candidates(*args):
        return [{}], {}

    monkeypatch.setattr(admin_storage.purge, "candidates", candidates)
    await storage.refresh_cleanup_count()
    known = await storage.cleanup_count()

    async def unavailable(*args):
        raise NotesFilesystemUnavailable("test unavailable")

    monkeypatch.setattr(admin_storage.purge, "candidates", unavailable)
    await storage.refresh_cleanup_count()
    assert await storage.cleanup_count() == known


async def test_refreshes_never_compute_in_parallel(storage, monkeypatch):
    active = 0
    maximum = 0

    async def candidates(*args):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return [], {}

    monkeypatch.setattr(admin_storage.purge, "candidates", candidates)
    await asyncio.gather(*(storage.refresh_cleanup_count() for _ in range(4)))
    assert maximum == 1


async def test_cancelling_cleanup_wakes_background_count_without_counting_inline(
    storage, monkeypatch
):
    abandoned = []

    async def interrupted():
        return {"token": "a" * 64, "left": 3}

    async def abandon(request):
        abandoned.append(request.token)

    async def must_not_count(*args):
        raise AssertionError("a request started a count")

    monkeypatch.setattr(storage, "interrupted", interrupted)
    monkeypatch.setattr(storage.store, "abandon_purge", abandon)
    monkeypatch.setattr(admin_storage.purge, "candidates", must_not_count)
    assert await storage.cleanup_cancel({}) == {"left": 3}
    assert abandoned == ["a" * 64]
    assert storage._cleanup_changed.is_set()
