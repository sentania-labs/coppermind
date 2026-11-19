"""Diagnostics explain a stalled read without changing its response or deadline."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from coppermind_store import request_timing
from coppermind_store.auth import InternalAuth
from coppermind_store.main import create_app

from coppermind import store_client
from coppermind.settings import Wiring
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import StoreUnavailable


@pytest.fixture
def events(monkeypatch):
    records = []

    def record(event, **fields):
        records.append(dict(event=event, **fields))

    logger = SimpleNamespace(info=record, warning=record)
    monkeypatch.setattr(request_timing, "log", logger)
    monkeypatch.setattr(store_client, "log", logger)
    return records


async def test_request_start_is_visible_before_completion_and_correlates_with_client(
    tmp_path, events
):
    app = create_app(Wiring(data_dir=tmp_path))
    entered, release = asyncio.Event(), asyncio.Event()

    async def read(payload):
        entered.set()
        await release.wait()
        return {"ok": True}

    app.state.auth = InternalAuth("test-token")
    app.state.store = SimpleNamespace(context=SimpleNamespace(lifecycle_detail=read))
    client = HttpStoreClient("http://store", "test-token", transport=httpx.ASGITransport(app))
    pending = asyncio.create_task(client.context_action("lifecycle", {"note_id": "private-note"}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert len(events) == 1
        assert events[0]["event"] == "store request started"
        identity = events[0]["request_id"]
        assert len(identity) == 32
        assert not pending.done()
        release.set()
        assert await pending == {"ok": True}
        assert events[1]["event"] == "store request finished"
        assert events[1]["request_id"] == identity
        assert events[1]["status"] == 200
        assert events[1]["outcome"] == "completed"
        assert "test-token" not in str(events)
        assert "private-note" not in str(events)
        await client.context_action("lifecycle", {})
        assert events[2]["request_id"] != identity
        assert request_timing.request_id.get() == ""
    finally:
        release.set()
        await pending
        await client.aclose()


async def test_slow_watchdog_names_the_step_before_the_operation_finishes(events, monkeypatch):
    monkeypatch.setattr(request_timing, "SLOW_SECONDS", 0.001)
    token = request_timing.request_id.set("a" * 32)
    try:
        with (
            request_timing.SlowOperation("review.detail") as timing,
            timing.step("get_note"),
        ):
            await asyncio.sleep(0.02)
            assert events[0]["outcome"] == "waiting"
            assert events[0]["active_step"] == "get_note"
            assert events[0]["active_step_ms"] > 0
        assert events[-1]["outcome"] == "completed"
        assert events[-1]["steps_ms"]["get_note"] > 0
        assert all(row["request_id"] == "a" * 32 for row in events)
    finally:
        request_timing.request_id.reset(token)


async def test_fast_operation_does_not_emit_slow_log(events):
    with request_timing.SlowOperation("review.detail") as timing, timing.step("job_scan"):
        pass
    assert not events


async def test_slow_failed_step_keeps_exception_and_duration(events, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(request_timing, "monotonic", lambda: clock[0])
    with (
        pytest.raises(ValueError, match="original"),
        request_timing.SlowOperation("review.detail") as timing,
        timing.step("evidence"),
    ):
        clock[0] = 3.0
        raise ValueError("original")
    assert events[-1]["outcome"] == "error"
    assert events[-1]["steps_ms"] == {"evidence": 3000.0}
    assert "original" not in str(events)


async def test_executor_queue_time_is_separate_from_read_time(events, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(request_timing, "monotonic", lambda: clock[0])

    async def queued(call):
        clock[0] = 1.0
        return call()

    def read():
        clock[0] = 3.0
        return "unchanged"

    monkeypatch.setattr(request_timing.asyncio, "to_thread", queued)
    with request_timing.SlowOperation("admin.overview") as timing, timing.step("files"):
        assert await timing.to_thread("calendar_files", read) == "unchanged"
    assert events[-1]["executor_wait_ms"] == {"calendar_files": 1000.0}
    assert events[-1]["steps_ms"] == {"files": 3000.0}


@pytest.mark.parametrize(
    ("exception", "phase"),
    [
        (httpx.ConnectTimeout, "connect"),
        (httpx.PoolTimeout, "pool"),
        (httpx.ReadTimeout, "read"),
        (httpx.WriteTimeout, "write"),
    ],
)
async def test_timeout_logs_phase_without_credentials_or_request_content(events, exception, phase):
    def timeout(request):
        raise exception("private transport details", request=request)

    client = HttpStoreClient(
        "http://user:private-password@store",
        "private-token",
        transport=httpx.MockTransport(timeout),
    )
    try:
        with pytest.raises(StoreUnavailable):
            await client.context_action("lifecycle", {"note_id": "private-note"})
        assert events[0]["event"] == "store request timed out"
        assert events[0]["phase"] == phase
        assert events[0]["path"] == "/internal/v1/context/lifecycle"
        assert len(events[0]["request_id"]) == 32
        assert "private" not in str(events)
        assert client._client.timeout.read == 30.0
    finally:
        await client.aclose()
