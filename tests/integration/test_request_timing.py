"""Real overview and review handlers expose slow read steps in correlated logs."""

import httpx
import pytest
import structlog
from coppermind_store import request_timing
from coppermind_store.auth import InternalAuth
from coppermind_store.main import create_app
from structlog.testing import capture_logs

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import CreateNote


@pytest.mark.parametrize("operation", ["overview", "lifecycle"])
async def test_slow_handler_logs_read_steps_without_changing_response(
    store, wiring, monkeypatch, operation
):
    await store.admin.startup()
    note = await store.create_note(CreateNote(title="Timing diagnostic"))
    clock = [0.0]
    monkeypatch.setattr(request_timing, "monotonic", lambda: clock[0])
    method = "get_problems" if operation == "overview" else "get_note"
    original = getattr(store, method)

    async def slow(*args, **kwargs):
        result = await original(*args, **kwargs)
        clock[0] += 3
        return result

    monkeypatch.setattr(store, method, slow)
    app = create_app(wiring)
    app.state.store = store
    app.state.auth = InternalAuth("test-token")
    structlog.configure(cache_logger_on_first_use=False)
    monkeypatch.setattr(request_timing, "log", structlog.get_logger("coppermind-store"))
    client = HttpStoreClient("http://store", "test-token", transport=httpx.ASGITransport(app))
    try:
        with capture_logs() as records:
            response = (
                await client.admin_action("overview", {})
                if operation == "overview"
                else await client.context_action("lifecycle", {"note_id": note.id})
            )
        assert "notes" in response if operation == "overview" else response["note"]["id"] == note.id
        start = next(row for row in records if row["event"] == "store request started")
        finish = next(row for row in records if row["event"] == "store request finished")
        slow_log = next(row for row in records if row["event"] == "slow store operation")
        assert start["request_id"] == slow_log["request_id"] == finish["request_id"]
        assert slow_log["steps_ms"]["problems" if operation == "overview" else "get_note"] == 3000
        assert slow_log["outcome"] == "completed"
        assert finish["status"] == 200
        if operation == "overview":
            assert set(slow_log["steps_ms"]) == {
                "problems",
                "services",
                "sugg",
                "trans",
                "search",
                "incoming",
                "calendar",
                "files",
                "apps",
                "waiting",
                "stuck",
                "companion_placed",
                "recent",
                "sync",
                "incoming_failed",
                "setup",
                "notices",
                "drop_folder",
                "timezone",
            }
        else:
            assert {
                "job_scan",
                "entities",
                "evidence",
                "collisions",
                "calendar_and_recording",
            } <= set(slow_log["steps_ms"])
    finally:
        await client.aclose()
