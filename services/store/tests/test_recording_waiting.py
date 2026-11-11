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
    recordings.assignment = lambda: SimpleNamespace(
        enabled=True, paused=False, connection="c", model="m"
    )
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
    recordings.assignment = lambda: SimpleNamespace(
        enabled=True, paused=False, connection="c", model="m"
    )
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
    failed = job(1, state="failed", error_rule="transcription-http", dismissed=True)
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
        error_rule="transcription-http",
        retry_after="2026-11-11T12:00:00+00:00",
        attempts=1,
        attempt_limit=2,
    )
    final = job(2, state="failed", error_rule="transcription-http", attempts=2)
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


@pytest.mark.asyncio
async def test_rejected_note_shows_the_stores_message_as_details(tmp_path):
    stored = "The note could not be filed: type: invalid; frontmatter: account is required."
    failure = job(
        1,
        state="failed",
        error_rule="note-rejected",
        transcribed=True,
        error=stored,
        field_problems=[{"field": "account", "reason": "should be a list"}],
    )
    recordings, _, _ = waiting_recordings(tmp_path, [failure])
    row = (await recordings.waiting())["failed"][0]
    assert row["field_problems"] == [{"field": "account", "reason": "should be a list"}]
    assert stored not in str(row)
    assert "Retry will only work after the vault's field rules are changed" in row["message"]
    assert "send the recording again with corrected fields" in row["message"]
    assert stored not in row["message"]
    assert row["can_retry"] is True


@pytest.mark.asyncio
async def test_rejected_note_without_details_falls_back_when_recheck_fails(tmp_path):
    failure = job(1, state="failed", error_rule="note-rejected")
    recordings, _, _ = waiting_recordings(tmp_path, [failure])
    row = (await recordings.waiting())["failed"][0]
    assert row["field_problems"] == []
    assert row["field_problems_unavailable"] is True


@pytest.mark.asyncio
async def test_every_refusal_that_a_retry_would_repeat_gets_its_own_next_step(tmp_path):
    from coppermind_store.recordings import NO_AUTOMATIC_RETRY

    rules = sorted(NO_AUTOMATIC_RETRY) + ["transcription-http-401"]
    failures = [job(i, state="failed", error_rule=rule) for i, rule in enumerate(rules, 1)]
    recordings, _, _ = waiting_recordings(tmp_path, failures)
    rows = {item["id"]: item for item in (await recordings.waiting())["failed"]}
    for failure in failures:
        message = rows[failure.id]["message"]
        assert "Try the recording again" not in message, failure.error_rule
        assert "Transcription could not finish" not in message, failure.error_rule
        assert "Turn on" not in message, failure.error_rule
    too_long = rows[failures[rules.index("transcription-too_long")].id]
    assert "shorter recording" in too_long["message"] and too_long["can_retry"] is False
    unfinished = rows[failures[rules.index("transcription-unfinished")].id]
    assert "settings, then retry" in unfinished["message"] and unfinished["can_retry"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled, model", [(False, "m"), (True, "")])
async def test_retry_advice_waits_for_transcription_to_be_set_up(tmp_path, enabled, model):
    failures = [
        job(1, state="failed", error_rule="connection-missing"),
        job(2, state="failed", error_rule="transcription-http"),
        job(3, state="failed", error_rule="transcription-no_speech"),
    ]
    recordings, _, _ = waiting_recordings(tmp_path, failures, enabled=enabled)
    recordings.assignment = lambda: SimpleNamespace(
        enabled=enabled, paused=False, connection="c", model=model
    )
    rows = {item["id"]: item for item in (await recordings.waiting())["failed"]}
    for failure in failures[:2]:
        assert rows[failure.id]["message"].endswith(
            "Set up and turn on transcription before retrying."
        )
        assert rows[failure.id]["can_retry"] is False
    assert "Set up and turn on" not in rows[failures[2].id]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("paused, enabled", [(True, True), (False, False)])
async def test_a_deferred_retry_cannot_be_dismissed_and_runs_when_transcription_resumes(
    tmp_path, paused, enabled
):
    from coppermind_store.model_provider import ProviderError

    pending = job(
        1,
        state="failed",
        error_rule="transcription-timeout",
        retry_after="2026-01-01T00:00:00+00:00",
        attempts=1,
        attempt_limit=2,
    )
    recordings, by_id, saved = waiting_recordings(tmp_path, [pending], enabled=enabled)
    state = SimpleNamespace(enabled=enabled, paused=paused, connection="c", model="m")
    recordings.assignment = lambda: state
    row = (await recordings.waiting())["failed"][0]
    assert row["can_dismiss"] is False
    with pytest.raises(ValueError, match="by itself"):
        await recordings.dismiss({"id": pending.id})
    assert not saved

    async def transcribe(item):
        raise ProviderError("The service did not answer.", "transcription-timeout")

    recordings.transcribe = transcribe
    assert await recordings.tick() is False
    state.enabled, state.paused = True, False
    assert await recordings.tick() is True
    assert saved[pending.id].attempts == 2


@pytest.mark.asyncio
async def test_waiting_skips_meetings_whose_audio_is_already_removed_after_review():
    notes = {"open": SimpleNamespace(title="Open", path="Open.md", frontmatter={})}
    store = SimpleNamespace(
        get_note=AsyncMock(side_effect=lambda identity: notes[identity]),
        control=SimpleNamespace(schema=lambda: SimpleNamespace(role=lambda _: "reviewed")),
    )
    recordings = Recordings(SimpleNamespace(store=store))
    recordings.assignment = lambda: None
    recordings.jobs = lambda: [
        job(1, state="succeeded", note_id="open"),
        job(2, state="succeeded", note_id="done", audio_removed=True),
    ]
    result = await recordings.waiting()
    assert [m["title"] for m in result["meetings"]] == ["Open"]
    store.get_note.assert_awaited_once_with("open")
