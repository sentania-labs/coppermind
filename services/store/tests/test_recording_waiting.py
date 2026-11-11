from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from coppermind_store.recordings import Recordings, TranscriptionJob


def job(i, **changes):
    values = dict(
        id=f"{i:064x}",
        artifact_id="a",
        filename="call.wav",
        mime_type="audio/wav",
        size_bytes=1,
        title="Call",
        provider="test",
        external_source_id=str(i),
        created_at="2026-01-01T00:00:00Z",
    )
    values.update(changes)
    return TranscriptionJob(**values)


@pytest.mark.asyncio
async def test_waiting_lists_all_current_notes_and_safe_failure_guidance():
    notes = {
        "one": SimpleNamespace(title="Renamed", path="Review/Renamed.md", frontmatter={}),
        "two": SimpleNamespace(title="Done", path="Done.md", frontmatter={"reviewed": True}),
    }
    store = SimpleNamespace(
        get_note=AsyncMock(side_effect=lambda identity: notes[identity]),
        control=SimpleNamespace(schema=lambda: SimpleNamespace(role=lambda _: "reviewed")),
    )
    recordings = Recordings(SimpleNamespace(store=store))
    recordings.assignment = lambda: SimpleNamespace(enabled=True, paused=False)
    recordings.jobs = lambda: [
        job(1, state="succeeded", note_id="one"),
        job(2, state="succeeded", note_id="two"),
        *[job(i, state="failed", error="secret raw error") for i in range(3, 105)],
        job(105, state="failed", error_rule="transcription-no_speech"),
    ]
    result = await recordings.waiting()
    assert result["meetings"] == [{"title": "Renamed", "path": "Review/Renamed.md", "date": ""}]
    assert len(result["failed"]) == 103
    assert all("secret" not in item["message"] for item in result["failed"])
    assert result["failed"][-1]["can_retry"] is False
    assert "No speech" in result["failed"][-1]["message"]


@pytest.mark.asyncio
async def test_waiting_survives_one_unreadable_note_and_offers_no_retry_for_missing_audio():
    from coppermind.store_protocol import StoreError

    notes = {"ok": SimpleNamespace(title="Ok", path="Ok.md", frontmatter={})}

    async def get_note(identity):
        if identity not in notes:
            raise StoreError("store unavailable")
        return notes[identity]

    store = SimpleNamespace(
        get_note=AsyncMock(side_effect=get_note),
        control=SimpleNamespace(schema=lambda: SimpleNamespace(role=lambda _: "reviewed")),
    )
    recordings = Recordings(SimpleNamespace(store=store))
    recordings.assignment = lambda: SimpleNamespace(enabled=True, paused=False)
    recordings.jobs = lambda: [
        job(1, state="succeeded", note_id="broken"),
        job(2, state="succeeded", note_id="ok"),
        job(3, state="failed", error_rule="recording-missing"),
    ]
    result = await recordings.waiting()
    assert [m["title"] for m in result["meetings"]] == ["Ok"]
    assert len(result["failed"]) == 1
    assert result["failed"][0]["can_retry"] is False
