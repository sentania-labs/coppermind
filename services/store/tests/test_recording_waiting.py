import asyncio
from types import SimpleNamespace
from typing import Any
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


def waiting_recordings(tmp_path, jobs, enabled=True):
    store = SimpleNamespace(
        get_note=AsyncMock(),
        control=SimpleNamespace(schema=lambda: SimpleNamespace(role=lambda _: "reviewed")),
    )
    lock = asyncio.Lock()
    saved: dict[str, Any] = {}
    context = SimpleNamespace(
        store=store,
        lock=lock,
        config=lambda: SimpleNamespace(
            assignments={
                "transcription": SimpleNamespace(
                    enabled=enabled, paused=False, connection="c", model="m"
                )
            }
        ),
    )
    recordings = Recordings(context)
    recordings.assignment = lambda: SimpleNamespace(
        enabled=enabled, paused=False, connection="c", model="m"
    )
    by_id = {item.id: item for item in jobs}
    recordings.jobs = lambda: list(by_id.values())
    recordings.load = lambda identity: by_id[identity]
    recordings.save = lambda item: saved.__setitem__(item.id, item.model_copy())
    return recordings, by_id, saved


@pytest.mark.asyncio
async def test_dismissed_failures_leave_the_list_and_the_count(tmp_path):
    first, second = (
        job(1, state="failed", error_rule="transcription-no_speech"),
        job(2, state="failed", error_rule="transcription-no_speech"),
    )
    recordings, by_id, saved = waiting_recordings(tmp_path, [first, second])
    assert len((await recordings.waiting())["failed"]) == 2
    assert await recordings.dismiss({"id": first.id}) == {"job": first.id}
    remaining = (await recordings.waiting())["failed"]
    assert [item["id"] for item in remaining] == [second.id]
    assert saved[first.id].dismissed is True and saved[first.id].state == "failed"


@pytest.mark.asyncio
async def test_only_a_failed_recording_can_be_dismissed(tmp_path):
    ok = job(1, state="succeeded")
    recordings, _, saved = waiting_recordings(tmp_path, [ok])
    with pytest.raises(ValueError, match="only a failed recording"):
        await recordings.dismiss({"id": ok.id})
    assert not saved


@pytest.mark.asyncio
async def test_retry_puts_a_dismissed_recording_back_on_the_list_if_it_fails_again(tmp_path):
    failed = job(1, state="failed", error_rule="transcription-http-500", dismissed=True)
    recordings, by_id, saved = waiting_recordings(tmp_path, [failed])
    recordings.ready = lambda: None
    assert (await recordings.waiting())["failed"] == []
    await recordings.retry({"id": failed.id})
    assert saved[failed.id].state == "queued" and saved[failed.id].dismissed is False
    by_id[failed.id].state = "failed"
    assert len((await recordings.waiting())["failed"]) == 1


@pytest.mark.asyncio
async def test_missing_audio_says_to_resend_unless_the_transcript_is_kept(tmp_path):
    gone = job(1, state="failed", error_rule="recording-missing")
    kept = job(2, state="failed", error_rule="recording-missing", transcribed=True)
    recordings, _, _ = waiting_recordings(tmp_path, [gone, kept])
    by_id = {item["id"]: item for item in (await recordings.waiting())["failed"]}
    assert by_id[gone.id]["message"] == (
        "The stored recording is missing. Send the recording again "
        "the same way recordings normally arrive."
    )
    assert by_id[gone.id]["can_retry"] is False
    assert "transcript is kept" in by_id[kept.id]["message"]
    assert "Send the recording again" not in by_id[kept.id]["message"]
    assert by_id[kept.id]["can_retry"] is True


@pytest.mark.asyncio
async def test_dismiss_is_refused_while_the_app_will_retry_by_itself(tmp_path):
    pending = job(
        1,
        state="failed",
        error_rule="transcription-http-500",
        retry_after="2026-11-11T12:00:00+00:00",
        attempts=1,
        attempt_limit=2,
    )
    final = job(2, state="failed", error_rule="transcription-http-500", attempts=2)
    recordings, _, saved = waiting_recordings(tmp_path, [pending, final])
    rows = {item["id"]: item for item in (await recordings.waiting())["failed"]}
    assert rows[pending.id]["can_dismiss"] is False
    assert "try again automatically" in rows[pending.id]["message"]
    assert rows[final.id]["can_dismiss"] is True
    with pytest.raises(ValueError, match="by itself"):
        await recordings.dismiss({"id": pending.id})
    assert not saved
    await recordings.dismiss({"id": final.id})
    assert saved[final.id].dismissed is True


@pytest.mark.asyncio
async def test_a_dismissed_recording_is_still_run_by_the_worker_and_shows_a_new_failure(tmp_path):
    from coppermind_store.model_provider import ProviderError

    dismissed = job(
        1, state="failed", error_rule="transcription-timeout", attempts=1, dismissed=True
    )
    recordings, by_id, saved = waiting_recordings(tmp_path, [dismissed])

    async def transcribe(item):
        raise ProviderError("The service did not answer.", "transcription-timeout")

    recordings.transcribe = transcribe
    assert (await recordings.waiting())["failed"] == []
    assert await recordings.tick() is True
    assert saved[dismissed.id].state == "failed" and saved[dismissed.id].attempts == 2
    assert saved[dismissed.id].dismissed is False
    assert len((await recordings.waiting())["failed"]) == 1
