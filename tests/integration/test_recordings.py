"""Audio-only intake against PostgreSQL and real files, with a slow stand-in transcriber.

No transcription service is reachable from the test environment, so the service here is a
local stand-in (`services.store.tests.test_transcription_call.Service`) that returns the
documented `diarized_json` shape after a delay, over a real socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta

import pytest
from coppermind_store import filing, staged
from coppermind_store.reconciler import reconcile_once
from coppermind_store.recordings import render_transcript

from coppermind.context_config import FeatureAssignment
from coppermind.db.models import Note
from coppermind.store_protocol import (
    CreateArtifact,
    CreateNote,
    PatchFrontmatter,
    RestoreNote,
    StagedReservation,
    ValidationFailed,
)
from services.store.tests.test_transcription_call import REPLY, Service
from tests.integration.test_note_recovery import commit, git
from tests.integration.test_review_filing import approve, drain

AUDIO = bytes(range(256)) * 700


async def chunks(data: bytes, size: int = 65536):
    for start in range(0, len(data), size):
        yield data[start : start + size]


async def store_audio(store, name="standup.m4a", data=AUDIO, mime="audio/mp4"):
    """The recording goes up the staged way, as n8n sends it."""
    receipt = await staged.reserve(
        store,
        StagedReservation.model_validate(
            {
                "filename": name,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "fields": {"mime_type": mime, "origin": "n8n"},
            }
        ),
    )
    await staged.put_bytes(store, receipt.id, chunks(data), declared_bytes=len(data))
    done = await staged.finalize(store, receipt.id)
    assert done.artifact is not None
    return done.artifact.id


async def store_audio_revision(store, artifact_id, data, name="standup.m4a"):
    receipt = await staged.reserve(
        store,
        StagedReservation.model_validate(
            {
                "filename": name,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "fields": {"mime_type": "audio/mp4", "artifact_id": artifact_id},
            }
        ),
    )
    await staged.put_bytes(store, receipt.id, chunks(data), declared_bytes=len(data))
    await staged.finalize(store, receipt.id)


async def setup_transcription(store, service, *, enabled=True, wait_s=30, **extra):
    context = store.context
    saved = await context.save_connection(
        {
            "revision": 0,
            "connection": {
                "name": "Speech",
                "provider": "compatible",
                "base_url": service.connection.base_url,
            },
        }
    )
    assignment = FeatureAssignment(
        connection=saved["saved"],
        model="whisper-diarize",
        enabled=enabled,
        wait_s=wait_s,
        **extra,
    )
    await context.save_transcription({"revision": 1, "assignment": assignment.model_dump()})
    return context


def request(artifact_id, **overrides):
    return {
        "artifact_id": artifact_id,
        "provider": "n8n",
        "external_source_id": "plaud/rec-1",
        "title": "Standup",
        "captured_at": "2026-11-09T09:30:00-06:00",
        "frontmatter": {"date": "2026-11-09T09:30:00"},
        **overrides,
    }


async def test_a_recording_alone_becomes_a_transcribed_note_in_review(store):
    async with Service(delay=0.4) as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        accepted = await context.accept_recording(request(audio))
        assert accepted["state"] == "queued" and accepted["artifact_id"] == audio

        assert await context.recordings.tick() is True
    # The recording went out whole, as one upload.
    assert AUDIO in service.requests[0]["body"]

    done = await context.recording_status({"id": accepted["id"]})
    assert done["state"] == "succeeded" and done["error"] == ""
    note = await store.get_note(done["note_id"])
    assert note.path.startswith("Review/") and note.frontmatter["reviewed"] is False
    assert audio in note.frontmatter["artifacts"]

    # Without returned signatures, speakers remain local to this recording.
    sources = await store.get_note_sources(note.id)
    assert len(sources) == 1 and sources[0].id == done["source_id"]
    source = await store.get_source(sources[0].id)
    assert {a.name for a in source.revisions[-1].artifacts} == {"transcript.txt", "transcript.json"}
    text = (await store.get_source_artifact(sources[0].id, 1, "transcript.txt")).content
    assert text == "[00:00:00] Speaker 1: Hello there.\n[00:00:03] Speaker 2: Hi.\n"
    segments = json.loads(
        (await store.get_source_artifact(sources[0].id, 1, "transcript.json")).content
    )["segments"]
    assert [s["speaker"] for s in segments] == ["Speaker 1", "Speaker 2"]

    # Review has not happened, so the audio is still stored.
    kept = await store.get_artifact_bytes(audio)
    assert await drain(kept) == AUDIO


async def test_reviewed_recording_is_removed_with_a_receipt_and_transcript(store):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        accepted = await context.accept_recording(
            request(audio, metadata={"archive_url": "https://example.test/archive/one"})
        )
        await context.recordings.tick()
    done = await context.recording_status({"id": accepted["id"]})
    archive = store.artifacts_root / audio / "r0001" / "standup.m4a"
    note = await store.get_note(done["note_id"])
    assert await context.recordings.tick() is False
    assert archive.read_bytes() == AUDIO
    await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"reviewed": True}), note.content_hash
    )
    assert await context.recordings.tick() is False
    assert archive.exists()
    assert await filing.file_reviewed(store) == {"filed": 1, "filing_failed": 0}
    assert await context.recordings.tick() is True
    assert not archive.exists()
    note = await store.get_note(note.id)
    detail = await context.recording_detail({"id": accepted["id"]})
    listing = (await context.snapshot())["recordings"]
    listed = next(item for item in listing if item["id"] == accepted["id"])
    assert detail["note_status"] == listed["note_status"] == "filed"
    assert detail["note_path"] == listed["note_path"] == note.path
    assert note.frontmatter["type"] == "reference"
    year, month = note.path.split("/")[1:3]
    assert note.path == f"reference/{year}/{month}/Standup.md"
    assert re.fullmatch(r"\d{4}", year) and re.fullmatch(rf"{year}-\d{{2}}", month)
    assert "Audio was removed after review" in note.body
    assert hashlib.sha256(AUDIO).hexdigest() in note.body
    assert str(len(AUDIO)) in note.body and "7.5" in note.body
    assert "https://example.test/archive/one" in note.body
    assert "[[Artifacts/" not in note.body
    assert audio not in note.frontmatter.get("artifacts", [])
    assert (await store.get_source_artifact(done["source_id"], 1, "transcript.txt")).content
    assert context.recordings.load(accepted["id"]).audio_removed
    assert await context.recordings.tick() is False


async def test_rebuilding_metadata_after_audio_removal_keeps_the_receipt(store):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        accepted = await context.accept_recording(request(audio))
        await context.recordings.tick()
    done = await context.recording_status({"id": accepted["id"]})
    await approve(store, done["note_id"])
    assert await context.recordings.tick() is True
    assert not (store.artifacts_root / audio / "r0001" / "standup.m4a").exists()
    await store.rebuild_metadata()
    manifest = json.loads((store.artifacts_root / audio / "manifest.json").read_text())
    assert manifest["removed_at"]
    assert manifest["revisions"][0]["sha256"] == hashlib.sha256(AUDIO).hexdigest()
    assert (await store.get_source_artifact(done["source_id"], 1, "transcript.txt")).content


def due_again(context, identity):
    job = context.recordings.load(identity)
    job.audio_removal_retry_after = ""
    context.recordings.save(job)


async def test_audio_removal_failure_retries_quietly_then_offers_a_working_retry(
    store, monkeypatch
):
    from coppermind_store import artifacts

    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        accepted = await context.accept_recording(request(audio))
        await context.recordings.tick()
    done = await context.recording_status({"id": accepted["id"]})
    await approve(store, done["note_id"])
    original = artifacts.remove_recording_audio

    async def fail(*args):
        raise OSError("stand-in disk failure")

    archive = store.artifacts_root / audio / "r0001" / "standup.m4a"
    monkeypatch.setattr(artifacts, "remove_recording_audio", fail)
    await context.recordings.tick()
    assert archive.exists()
    assert context.recordings.load(accepted["id"]).audio_removal_attempts == 1
    assert not [p for p in await store.get_problems() if p.kind == "recording"]
    assert await context.recordings.tick() is False
    for _ in range(2):
        due_again(context, accepted["id"])
        await context.recordings.tick()
    stuck = [p for p in await store.get_problems() if p.kind == "recording"]
    assert len(stuck) == 1 and "could not be removed" in stuck[0].reason
    assert stuck[0].retry_available is True
    listed = next(j for j in context.recordings.listing() if j["id"] == accepted["id"])
    assert listed["can_retry"] and listed["audio_removal_stuck"]
    due_again(context, accepted["id"])
    assert await context.recordings.tick() is False

    monkeypatch.setattr(artifacts, "remove_recording_audio", original)
    await context.retry_recording({"id": accepted["id"]})
    assert await context.recordings.tick() is True
    assert context.recordings.load(accepted["id"]).audio_removed
    assert not archive.exists()
    assert not [p for p in await store.get_problems() if p.kind == "recording"]


async def test_a_note_deleted_after_review_does_not_keep_the_audio(store):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        accepted = await context.accept_recording(request(audio))
        await context.recordings.tick()
    done = await context.recording_status({"id": accepted["id"]})
    await approve(store, done["note_id"])
    async with store.session_factory() as session, session.begin():
        (await session.get(Note, done["note_id"])).state = "deleted"
    assert await context.recordings.tick() is True
    assert not (store.artifacts_root / audio / "r0001" / "standup.m4a").exists()
    job = context.recordings.load(accepted["id"])
    assert job.audio_removed and not job.audio_removal_error
    assert not [p for p in await store.get_problems() if p.kind == "recording"]


async def set_note_state(store, note_id, state):
    async with store.session_factory() as session, session.begin():
        (await session.get(Note, note_id)).state = state


async def test_a_note_deleted_before_review_keeps_its_audio_until_it_is_purged(store):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        accepted = await context.accept_recording(request(audio))
        await context.recordings.tick()
    done = await context.recording_status({"id": accepted["id"]})
    archive = store.artifacts_root / audio / "r0001" / "standup.m4a"
    await set_note_state(store, done["note_id"], "deleted")
    for _ in range(2):
        assert await context.recordings.tick() is False
    assert archive.read_bytes() == AUDIO
    assert not context.recordings.load(accepted["id"]).audio_removed

    await set_note_state(store, done["note_id"], "ok")
    assert await context.recordings.tick() is False
    assert archive.read_bytes() == AUDIO
    await approve(store, done["note_id"])
    assert await context.recordings.tick() is True
    assert not archive.exists()
    assert "Audio was removed after review" in (await store.get_note(done["note_id"])).body


async def test_a_note_purged_before_review_takes_its_audio_with_it(store):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        accepted = await context.accept_recording(request(audio))
        await context.recordings.tick()
    done = await context.recording_status({"id": accepted["id"]})
    archive = store.artifacts_root / audio / "r0001" / "standup.m4a"
    note_path = (await store.get_note(done["note_id"])).path
    await set_note_state(store, done["note_id"], "deleted")
    assert await context.recordings.tick() is False
    assert archive.exists()
    (store.notes_root / note_path).unlink()
    async with store.session_factory() as session, session.begin():
        await session.delete(await session.get(Note, done["note_id"]))
    assert await context.recordings.tick() is True
    assert not archive.exists()
    assert context.recordings.load(accepted["id"]).audio_removed
    assert not [p for p in await store.get_problems() if p.kind == "recording"]


async def test_an_empty_database_does_not_make_unreviewed_notes_look_purged(store):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        other = await store_audio(store, name="other.m4a", data=AUDIO + b"2")
        first = await context.accept_recording(request(audio, title="First"))
        second = await context.accept_recording(
            request(other, title="Second", external_source_id="plaud/rec-2")
        )
        await context.recordings.tick()
        await context.recordings.tick()
    notes = [
        (await context.recording_status({"id": item["id"]}))["note_id"] for item in (first, second)
    ]
    archives = [
        store.artifacts_root / audio / "r0001" / "standup.m4a",
        store.artifacts_root / other / "r0001" / "other.m4a",
    ]
    async with store.session_factory() as session, session.begin():
        for note_id in notes:
            await session.delete(await session.get(Note, note_id))
    assert await context.recordings.tick() is False
    assert [path.read_bytes() for path in archives] == [AUDIO, AUDIO + b"2"]
    for item in (first, second):
        job = context.recordings.load(item["id"])
        assert not job.audio_removed and not job.audio_removal_error


async def test_shared_audio_stays_while_the_other_note_has_no_row_but_still_has_its_file(store):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        first = await context.accept_recording(request(audio, title="First"))
        second = await context.accept_recording(
            request(audio, title="Second", external_source_id="plaud/rec-2")
        )
        await context.recordings.tick()
        await context.recordings.tick()
    one = await context.recording_status({"id": first["id"]})
    two = await context.recording_status({"id": second["id"]})
    await approve(store, one["note_id"])
    async with store.session_factory() as session, session.begin():
        await session.delete(await session.get(Note, two["note_id"]))
    archive = store.artifacts_root / audio / "r0001" / "standup.m4a"
    assert await context.recordings.tick() is False
    assert archive.read_bytes() == AUDIO
    assert not context.recordings.load(first["id"]).audio_removed


async def test_a_purged_archive_still_settles_with_a_receipt_from_what_is_known(store):
    import shutil

    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        accepted = await context.accept_recording(
            request(audio, metadata={"archive_url": "https://example.test/archive/two"})
        )
        await context.recordings.tick()
    done = await context.recording_status({"id": accepted["id"]})
    await store.remove_artifact(audio)
    shutil.rmtree(store.artifacts_root / audio)
    await approve(store, done["note_id"])
    assert await context.recordings.tick() is True
    job = context.recordings.load(accepted["id"])
    assert job.audio_removed and not job.audio_removal_error
    body = (await store.get_note(done["note_id"])).body
    assert "Audio was removed after review" in body
    assert f"size: {len(AUDIO)} bytes" in body and "7.5" in body
    assert "https://example.test/archive/two" in body
    assert hashlib.sha256(AUDIO).hexdigest() not in body
    assert audio not in (await store.get_note(done["note_id"])).frontmatter.get("artifacts", [])
    assert not [p for p in await store.get_problems() if p.kind == "recording"]


async def test_an_artifact_the_owner_removed_still_loses_its_audio_after_review(store):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        accepted = await context.accept_recording(request(audio))
        await context.recordings.tick()
    done = await context.recording_status({"id": accepted["id"]})
    archive = store.artifacts_root / audio / "r0001" / "standup.m4a"
    await store.remove_artifact(audio)
    assert archive.read_bytes() == AUDIO
    assert await context.recordings.tick() is False
    assert archive.read_bytes() == AUDIO

    await approve(store, done["note_id"])
    assert await context.recordings.tick() is True
    assert not archive.exists()
    job = context.recordings.load(accepted["id"])
    assert job.audio_removed and not job.audio_removal_error
    assert not [p for p in await store.get_problems() if p.kind == "recording"]
    manifest = json.loads((store.artifacts_root / audio / "manifest.json").read_text())
    assert manifest["removed_reason"] != "recording removed after review"
    assert manifest["revisions"][0]["sha256"] == hashlib.sha256(AUDIO).hexdigest()
    await store.rebuild_metadata()


async def test_a_recording_waiting_in_review_is_not_read_again_on_every_tick(store, monkeypatch):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        accepted = await context.accept_recording(request(audio))
        await context.recordings.tick()
    reads = []
    original = type(context.recordings).saved_transcript

    def counted(self, job):
        reads.append(job.id)
        return original(self, job)

    monkeypatch.setattr(type(context.recordings), "saved_transcript", counted)
    for _ in range(3):
        assert await context.recordings.tick() is False
    assert reads == []
    done = await context.recording_status({"id": accepted["id"]})
    await approve(store, done["note_id"])
    assert await context.recordings.tick() is True
    assert reads == [accepted["id"]]


async def test_audio_shared_with_another_unfinished_recording_is_kept_until_the_last_is_done(
    store,
):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        first = await context.accept_recording(request(audio, title="First"))
        second = await context.accept_recording(
            request(audio, title="Second", external_source_id="plaud/rec-2")
        )
        await context.recordings.tick()
        await context.recordings.tick()
    archive = store.artifacts_root / audio / "r0001" / "standup.m4a"
    one = await context.recording_status({"id": first["id"]})
    two = await context.recording_status({"id": second["id"]})
    await approve(store, one["note_id"])
    assert await context.recordings.tick() is False
    assert archive.read_bytes() == AUDIO
    kept = context.recordings.load(first["id"])
    assert kept.audio_kept and not kept.audio_removed and not kept.audio_removal_error
    assert not [p for p in await store.get_problems() if p.kind == "recording"]

    await approve(store, two["note_id"])
    while await context.recordings.tick():
        pass
    assert not archive.exists()
    assert context.recordings.load(first["id"]).audio_removed
    assert context.recordings.load(second["id"]).audio_removed
    assert "Audio was removed after review" in (await store.get_note(two["note_id"])).body


async def test_shared_audio_survives_deletion_and_restore_of_unreviewed_note(store):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        first = await context.accept_recording(request(audio, title="First"))
        second = await context.accept_recording(
            request(audio, title="Second", external_source_id="plaud/rec-2")
        )
        await context.recordings.tick()
        await context.recordings.tick()
    one = await context.recording_status({"id": first["id"]})
    two = await context.recording_status({"id": second["id"]})
    note = await store.get_note(two["note_id"])
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    (store.notes_root / note.path).unlink()
    await reconcile_once(store)
    commit(store.notes_root)
    await reconcile_once(store)
    await approve(store, one["note_id"])
    await context.recordings.tick()
    archive = store.artifacts_root / audio / "r0001" / "standup.m4a"
    assert archive.read_bytes() == AUDIO
    for path in store.notes_root.rglob("*"):
        if path.is_file() and ".git" not in path.relative_to(store.notes_root).parts:
            os.utime(path, (1, 1))
    preview = await store.preview_note_restore(note.id)
    await store.restore_note(
        note.id, RestoreNote(token=preview.token, confirmation=f"Restore {note.id}")
    )
    assert await drain(await store.get_artifact_bytes(audio)) == AUDIO
    await approve(store, note.id)
    while await context.recordings.tick():
        pass
    assert not archive.exists()


async def test_deleted_unreviewed_citing_note_keeps_audio_until_purged(store):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        accepted = await context.accept_recording(request(audio))
        await context.recordings.tick()
    other = await store.create_note(
        CreateNote(title="Other note", body="Shared", frontmatter={"artifacts": [audio]})
    )
    await set_note_state(store, other.id, "deleted")
    done = await context.recording_status({"id": accepted["id"]})
    await approve(store, done["note_id"])
    assert await context.recordings.tick() is False
    archive = store.artifacts_root / audio / "r0001" / "standup.m4a"
    assert archive.read_bytes() == AUDIO
    async with store.session_factory() as session, session.begin():
        await session.delete(await session.get(Note, other.id))
    assert await context.recordings.tick() is True
    assert not archive.exists()


async def test_audio_removal_resumes_after_bytes_are_already_gone(store, monkeypatch):
    from coppermind_store import artifacts

    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        accepted = await context.accept_recording(request(audio))
        await context.recordings.tick()
    done = await context.recording_status({"id": accepted["id"]})
    await approve(store, done["note_id"])
    original = artifacts.remove_recording_audio

    async def interrupted(*args):
        await original(*args)
        raise OSError("stand-in interruption after deletion")

    monkeypatch.setattr(artifacts, "remove_recording_audio", interrupted)
    await context.recordings.tick()
    assert not (store.artifacts_root / audio / "r0001" / "standup.m4a").exists()
    job = context.recordings.load(accepted["id"])
    assert job.audio_removal_error
    job.audio_removal_retry_after = ""
    context.recordings.save(job)
    monkeypatch.setattr(artifacts, "remove_recording_audio", original)
    await context.recordings.tick()
    assert context.recordings.load(accepted["id"]).audio_removed
    assert "Audio was removed after review" in (await store.get_note(done["note_id"])).body


async def test_empty_saved_transcript_or_changed_audio_is_never_removed(store):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        accepted = await context.accept_recording(request(audio))
        await context.recordings.tick()
    done = await context.recording_status({"id": accepted["id"]})
    await approve(store, done["note_id"])
    job = context.recordings.load(accepted["id"])
    saved = context.read(context.recordings.transcript_path(job), None)
    context.write(context.recordings.transcript_path(job), {**saved, "segments": []})
    archive = store.artifacts_root / audio / "r0001" / "standup.m4a"
    assert await context.recordings.tick() is False
    assert archive.read_bytes() == AUDIO

    context.write(context.recordings.transcript_path(job), saved)
    changed = b"changed on disk"
    archive.write_bytes(changed)
    assert await context.recordings.tick() is True
    assert archive.read_bytes() == changed
    job = context.recordings.load(accepted["id"])
    assert job.audio_removal_error and not job.audio_removed


async def test_the_same_request_is_one_intake_and_a_different_recording_is_refused(store):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        first = await context.accept_recording(request(audio))
        again = await context.accept_recording(request(audio))
        assert again["id"] == first["id"]
        other = await store_audio(store, "other.m4a", AUDIO[:5000])
        with pytest.raises(ValueError, match="different recording"):
            await context.accept_recording(request(other))
        assert len(context.recordings.jobs()) == 1


async def test_a_recording_is_kept_until_transcription_is_on_and_other_files_are_refused(store):
    async with Service() as service:
        context = store.context
        audio = await store_audio(store)
        # Handed in before any transcription service exists: kept, and nothing is sent.
        kept = await context.accept_recording(request(audio))
        await context.recordings.tick()
        assert context.recordings.load(kept["id"]).state == "queued"
        assert service.requests == []
        context = await setup_transcription(store, service)
        # Once transcription is on, the recording that waited is written up.
        await context.recordings.tick()
        assert context.recordings.load(kept["id"]).state == "succeeded"
        assert len(service.requests) == 1
        with pytest.raises(ValueError, match="no stored file"):
            await context.accept_recording(
                request("01ARZ3NDEKTSV4RRFFQ69G5FAV", external_source_id="plaud/missing")
            )
        note_file = await store.create_artifact(
            CreateArtifact(kind="artifact", filename="notes.txt", content_base64="aGk=")
        )
        with pytest.raises(ValueError, match="not a recording"):
            await context.accept_recording(request(note_file.id, external_source_id="plaud/text"))
        video = await store_audio(store, name="clip.mp4", data=AUDIO + b"v", mime="video/mp4")
        with pytest.raises(ValueError, match="video/mp4, not a recording"):
            await context.accept_recording(request(video, external_source_id="plaud/video"))


async def test_a_refusal_lands_in_problems_keeps_the_audio_and_retries_on_request(store):
    service = Service(status=404, reply={"error": "nope"})
    async with service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        job = await context.accept_recording(request(audio))
        await context.recordings.tick()
        failed = await context.recording_status({"id": job["id"]})
        assert failed["state"] == "failed" and failed["error_rule"] == "transcription-http-404"
        assert "could not find the endpoint" in failed["error"]
        # A refusal the same request would only repeat is not tried again by itself.
        assert await context.recordings.tick() is False
        assert len(service.requests) == 1

        problems = [p for p in await store.get_problems() if p.kind == "recording"]
        assert len(problems) == 1 and "could not find the endpoint" in problems[0].reason
        assert problems[0].reference == "Standup"
        assert await drain(await store.get_artifact_bytes(audio)) == AUDIO

        listed = (await context.snapshot())["recordings"]
        assert listed[0]["state"] == "failed" and listed[0]["can_retry"] is True

        service.status, service.reply = 200, REPLY
        await context.retry_recording({"id": job["id"]})
        assert (await context.recording_status({"id": job["id"]}))["state"] == "queued"
        await context.recordings.tick()
    done = await context.recording_status({"id": job["id"]})
    assert done["state"] == "succeeded" and done["note_id"]
    assert not [p for p in await store.get_problems() if p.kind == "recording"]


async def test_a_failed_recording_can_be_retried_when_the_service_needs_no_model_name(store):
    service = Service(status=404, reply={"error": "nope"})
    async with service:
        context = await setup_transcription(store, service)
        job = await context.accept_recording(request(await store_audio(store)))
        await context.recordings.tick()
        assert (await context.recording_status({"id": job["id"]}))["state"] == "failed"
        config = context.config()
        assignment = config.assignments["transcription"].model_copy(update={"model": ""})
        await context.save_transcription(
            {"revision": config.revision, "assignment": assignment.model_dump()}
        )
        (row,) = (await context.recording_waiting({}))["failed"]
        assert row["can_retry"] is True
        assert "Set up" not in row["message"] and "set up" not in row["message"]


async def test_an_unreadable_recording_is_not_reported_as_a_filing_failure(store, monkeypatch):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        job = await context.accept_recording(request(audio))

        async def unreadable(*args, **kwargs):
            raise PermissionError("denied")

        monkeypatch.setattr(context.provider, "transcribe", unreadable)
        await context.recordings.tick()
    failed = await context.recording_status({"id": job["id"]})
    assert failed["state"] == "failed" and failed["error_rule"] == "recording-unreadable"
    assert "could not be read" in failed["error"] and "Filing" not in failed["error"]
    assert not service.requests


async def test_an_unexpected_failure_before_a_transcript_backs_off_instead_of_resending(
    store, monkeypatch
):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        job = await context.accept_recording(request(audio))

        async def broken(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(context.provider, "transcribe", broken)
        await context.recordings.tick()
        failed = await context.recording_status({"id": job["id"]})
        assert (
            failed["error_rule"] == "job-error"
            and "Transcription stopped unexpectedly" in failed["error"]
        )
        assert datetime.fromisoformat(failed["retry_after"]) > datetime.now(UTC)
        assert await context.recordings.tick() is False
    assert not service.requests


async def test_a_silent_service_backs_off_before_the_next_attempt(store):
    async with Service(delay=6) as service:
        context = await setup_transcription(store, service, wait_s=5)
        audio = await store_audio(store)
        job = await context.accept_recording(request(audio))
        await context.recordings.tick()
    failed = await context.recording_status({"id": job["id"]})
    assert failed["error_rule"] == "provider-no-response"
    assert datetime.fromisoformat(failed["retry_after"]) > datetime.now(UTC) + timedelta(minutes=25)
    # Backing off: the worker leaves it alone until the time comes.
    assert await context.recordings.tick() is False


async def test_a_note_that_cannot_be_filed_keeps_its_transcript_and_is_not_sent_again(store):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        job = await context.accept_recording(request(audio))
        real = store.ingest
        calls = []

        async def refuse(ingest_request, **kwargs):
            calls.append(ingest_request)
            if len(calls) == 1:
                raise ValidationFailed(
                    ["frontmatter: type is required"],
                    field_problems=[{"field": "type", "reason": "is required"}],
                )
            return await real(ingest_request, **kwargs)

        store.ingest = refuse
        await context.recordings.tick()
        failed = await context.recording_status({"id": job["id"]})
        assert failed["error_rule"] == "note-rejected" and "type is required" in failed["error"]
        assert "transcript is kept" in failed["error"]
        row = (await context.recording_waiting({}))["failed"][0]
        assert row["field_problems"] == [{"field": "type", "reason": "is required"}]
        assert "Retry will only work after the vault's field rules are changed" in row["message"]
        assert "send the recording again with corrected fields" in row["message"]
        await context.retry_recording({"id": job["id"]})
        await context.recordings.tick()
    assert (await context.recording_status({"id": job["id"]}))["state"] == "succeeded"
    assert len(service.requests) == 1  # the recording was transcribed once, not twice


async def test_paused_or_off_transcription_leaves_a_recording_waiting(store):
    async with Service() as service:
        context = await setup_transcription(store, service, paused=True)
        audio = await store_audio(store)
        await context.accept_recording(request(audio))
        assert await context.recordings.tick() is False
        assert not service.requests


def rewind(context, job_id, **changes):
    job = context.recordings.load(job_id)
    for name, value in changes.items():
        setattr(job, name, value)
    context.recordings.save(job)


async def test_another_client_holding_the_service_is_a_wait_not_a_failed_attempt(store):
    async with Service(status=429, retry_after=1) as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        job = await context.accept_recording(request(audio))
        for _ in range(4):  # more busy answers than the two attempts allowed
            assert await context.recordings.tick() is True
            waiting = await context.recording_status({"id": job["id"]})
            assert waiting["state"] == "queued" and waiting["attempts"] == 0
            assert waiting["error_rule"] == "transcription-busy"
            assert "busy" in waiting["error"]
            asked_again = datetime.fromisoformat(waiting["retry_after"])
            until_then = (asked_again - datetime.now(UTC)).total_seconds()
            assert 50 < until_then <= 61  # a Retry-After of 1 second is held to the 60 s floor
            # Not asked again before its time, and it is no entry in Problems.
            assert await context.recordings.tick() is False
            assert not [p for p in await store.get_problems() if p.kind == "recording"]
            rewind(context, job["id"], retry_after=(datetime.now(UTC)).isoformat())
        assert len(service.requests) == 4

        service.status, service.reply = 200, REPLY
        assert await context.recordings.tick() is True
    done = await context.recording_status({"id": job["id"]})
    assert done["state"] == "succeeded" and done["attempts"] == 1 and done["error"] == ""
    assert done["note_id"]


async def test_a_busy_wait_has_a_default_of_a_couple_of_minutes(store):
    async with Service(status=429) as service:
        context = await setup_transcription(store, service)
        job = await context.accept_recording(request(await store_audio(store)))
        await context.recordings.tick()
    waiting = await context.recording_status({"id": job["id"]})
    delay = (datetime.fromisoformat(waiting["retry_after"]) - datetime.now(UTC)).total_seconds()
    assert 100 <= delay <= 190


async def test_a_day_of_continuous_busy_becomes_a_problem_with_a_retry_and_no_automatic_try(store):
    async with Service(status=429) as service:
        context = await setup_transcription(store, service)
        job = await context.accept_recording(request(await store_audio(store)))
        await context.recordings.tick()
        rewind(
            context,
            job["id"],
            busy_since=(datetime.now(UTC) - timedelta(hours=25)).isoformat(),
            retry_after="",
        )
        await context.recordings.tick()
        failed = await context.recording_status({"id": job["id"]})
        assert failed["state"] == "failed" and failed["error_rule"] == "transcription-busy-limit"
        assert "24 hours" in failed["error"]
        assert await context.recordings.tick() is False
        problems = [p for p in await store.get_problems() if p.kind == "recording"]
        assert len(problems) == 1 and problems[0].retry_available

        service.status, service.reply = 200, REPLY
        await context.retry_recording({"id": job["id"]})
        await context.recordings.tick()
    assert (await context.recording_status({"id": job["id"]}))["state"] == "succeeded"


async def test_a_restart_in_the_middle_of_a_transcription_then_a_busy_service_still_waits(store):
    async with Service(status=429) as service:
        context = await setup_transcription(store, service)
        job = await context.accept_recording(request(await store_audio(store)))
        # The store died on the last attempt it allows, with the upload still running.
        limit = context.recordings.load(job["id"]).attempt_limit
        rewind(context, job["id"], state="running", attempts=limit)
        assert await context.recordings.tick() is True
        waiting = await context.recording_status({"id": job["id"]})
        assert waiting["state"] == "queued" and waiting["attempts"] == limit - 1
        assert waiting["error_rule"] == "transcription-busy"

        service.status, service.reply = 200, REPLY
        rewind(context, job["id"], retry_after="")
        assert await context.recordings.tick() is True
    assert (await context.recording_status({"id": job["id"]}))["state"] == "succeeded"


@pytest.mark.parametrize("duration", [float("inf"), float("nan")])
async def test_a_non_finite_duration_is_treated_as_unknown(store, duration):
    async with Service() as service:
        context = await setup_transcription(store, service)
        job = await context.accept_recording(
            request(await store_audio(store), metadata={"duration_seconds": duration})
        )
        assert await context.recordings.tick() is True
    assert [r["path"].split(" ")[:2] for r in service.requests] == [
        ["POST", "/v1/audio/transcriptions"]
    ]
    assert (await context.recording_status({"id": job["id"]}))["state"] == "succeeded"


async def test_a_restart_on_the_jobs_path_resumes_the_saved_job(store, monkeypatch):
    from coppermind_store import model_provider

    monkeypatch.setattr(model_provider, "JOB_POLL_SECONDS", 0)
    async with Service(jobs=True) as service:
        context = await setup_transcription(store, service)
        job = await context.accept_recording(
            request(await store_audio(store), metadata={"duration_seconds": 7500})
        )
        rewind(
            context,
            job["id"],
            state="running",
            attempts=1,
            remote_job_id="local-job",
            remote_job_started=datetime.now(UTC).timestamp(),
        )
        assert await context.recordings.tick() is True
    assert len(service.requests) == 1
    assert service.requests[0]["path"].startswith("GET /v1/audio/transcriptions/jobs/local-job ")
    done = await context.recording_status({"id": job["id"]})
    assert done["state"] == "succeeded" and done["attempts"] == 1
    finished = context.recordings.load(job["id"])
    assert (finished.remote_job_id, finished.remote_job_started, finished.remote_job_resubmits) == (
        "",
        0,
        0,
    )


async def test_a_failed_job_retries_with_the_same_key_and_stops_at_the_cap(store, monkeypatch):
    from coppermind_store import model_provider

    monkeypatch.setattr(model_provider, "JOB_POLL_SECONDS", 0)
    monkeypatch.setattr(model_provider, "MIN_RETRY_AFTER_SECONDS", 0)
    async with Service(jobs=True, job_status="failed") as service:
        context = await setup_transcription(store, service)
        job = await context.accept_recording(
            request(await store_audio(store), metadata={"duration_seconds": 7500})
        )
        await context.recordings.tick()
        failed = await context.recording_status({"id": job["id"]})
        assert failed["state"] == "failed" and failed["error_rule"] == "transcription-unfinished"
        service.job_status = "done"
        await context.retry_recording({"id": job["id"]})
        await context.recordings.tick()
    keys = [
        r["headers"]["idempotency-key"] for r in service.requests if r["path"].startswith("POST")
    ]
    assert keys == [job["id"]] * (model_provider.JOB_MAX_RESUBMITS + 2)
    assert (await context.recording_status({"id": job["id"]}))["state"] == "succeeded"


async def test_a_service_busy_for_longer_than_the_job_budget_before_accepting_still_waits(
    store, monkeypatch
):
    from coppermind_store import model_provider

    monkeypatch.setattr(model_provider, "JOB_POLL_SECONDS", 0)
    monkeypatch.setattr(model_provider, "JOB_PACE_HEADROOM", 0)
    monkeypatch.setattr(model_provider, "JOB_MIN_SECONDS", 0.3)
    async with Service(jobs=True, status=429) as service:
        context = await setup_transcription(store, service)
        job = await context.accept_recording(
            request(await store_audio(store), metadata={"duration_seconds": 7500})
        )
        for _ in range(2):
            assert await context.recordings.tick() is True
            waiting = await context.recording_status({"id": job["id"]})
            assert waiting["state"] == "queued" and waiting["attempts"] == 0
            assert waiting["error_rule"] == "transcription-busy"
            assert context.recordings.load(job["id"]).remote_job_started == 0
            await asyncio.sleep(0.4)
            rewind(context, job["id"], retry_after=datetime.now(UTC).isoformat())
        service.status = 200
        assert await context.recordings.tick() is True
    done = await context.recording_status({"id": job["id"]})
    assert done["state"] == "succeeded" and done["attempts"] == 1
    assert len([r for r in service.requests if r["path"].startswith("POST")]) == 3


@pytest.mark.parametrize("job_status", ["running", None])
async def test_a_wedged_service_ends_as_a_retriable_problem_and_frees_the_worker(
    store, monkeypatch, job_status
):
    from coppermind_store import model_provider

    monkeypatch.setattr(model_provider, "JOB_POLL_SECONDS", 0.01)
    monkeypatch.setattr(model_provider, "JOB_PACE_HEADROOM", 0)
    monkeypatch.setattr(model_provider, "JOB_MIN_SECONDS", 0.3)
    async with Service(jobs=True, job_status=job_status) as service:
        context = await setup_transcription(store, service)
        wedged = await context.accept_recording(
            request(await store_audio(store), metadata={"duration_seconds": 7500})
        )
        await context.recordings.tick()
        failed = await context.recording_status({"id": wedged["id"]})
        assert failed["state"] == "failed" and failed["error_rule"] == "transcription-unfinished"
        assert "did not finish this recording" in failed["error"]
        # The worker is free for the next recording, and the failure waits for a person.
        other = await context.accept_recording(
            request(
                await store_audio(store, name="b.m4a", data=AUDIO + b"b"),
                external_source_id="plaud/rec-2",
                title="Other",
            )
        )
        service.jobs = False
        assert await context.recordings.tick() is True
        assert (await context.recording_status({"id": other["id"]}))["state"] == "succeeded"
        assert await context.recordings.tick() is False
        problems = [p for p in await store.get_problems() if p.kind == "recording"]
        assert len(problems) == 1 and problems[0].retry_available


def test_the_transcript_text_keeps_each_label_and_marks_a_missing_one():
    text = render_transcript(
        {
            "segments": [
                {"speaker": "Speaker 1", "start": 3725.0, "end": 3730.0, "text": "Late."},
                {"speaker": "", "start": None, "end": None, "text": "No label."},
            ]
        }
    )
    assert text == "[01:02:05] Speaker 1: Late.\nUnknown speaker: No label.\n"


async def test_a_newer_revision_of_the_artifact_is_not_transcribed_in_place_of_the_accepted_one(
    store,
):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        job = await context.accept_recording(request(audio))
        newer = AUDIO[::-1][:9000]
        await store_audio_revision(store, audio, newer)
        assert (await store.get_artifact(audio)).revision == 2
        await context.recordings.tick()
    assert (await context.recording_status({"id": job["id"]}))["state"] == "succeeded"
    assert AUDIO in service.requests[0]["body"] and newer not in service.requests[0]["body"]


async def test_a_retried_request_still_finds_its_intake_after_transcription_is_off(store):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        first = await context.accept_recording(request(audio))
        # Routine connection maintenance switches the assignment off.
        saved = context.config().connections[0]
        await context.save_connection(
            {
                "revision": context.config().revision,
                "connection": {**saved.model_dump(), "id": saved.id},
            }
        )
        assert context.config().assignments["transcription"].enabled is False
        again = await context.accept_recording(request(audio))
        assert again["id"] == first["id"]
        # A new recording is kept too, and waits for transcription to be turned on again.
        later = await context.accept_recording(request(audio, external_source_id="plaud/rec-2"))
        assert later["id"] != first["id"]
        assert context.recordings.load(later["id"]).state == "queued"


async def test_the_recording_page_reads_the_transcript_and_a_retry_needs_transcription_on(store):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        job = await context.accept_recording(request(audio))
        await context.recordings.tick()
        detail = await context.recording_detail({"id": job["id"]})
        assert detail["state"] == "succeeded" and detail["note_path"].startswith("Review/")
        assert detail["transcript"].startswith("[00:00:00] Speaker 1: Hello there.")
        assert "frontmatter" not in detail and detail["can_retry"] is False

        # A failed intake only offers a retry while transcription is switched on.
        stored = context.recordings.load(job["id"])
        stored.state = "failed"
        context.recordings.save(stored)
        assert (await context.recording_detail({"id": job["id"]}))["can_retry"] is True
        assert context.recordings.listing()[0]["can_retry"] is True
        config = context.config()
        config.assignments["transcription"].enabled = False
        context.save_config(config)
        assert (await context.recording_detail({"id": job["id"]}))["can_retry"] is False
        assert context.recordings.listing()[0]["can_retry"] is False


async def test_a_recording_problem_says_whether_a_retry_can_be_queued_now(store):
    service = Service(status=404, reply={"error": "nope"})
    async with service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        await context.accept_recording(request(audio))
        await context.recordings.tick()
        problem = next(p for p in await store.get_problems() if p.kind == "recording")
        assert problem.retry_available is True
        config = context.config()
        config.assignments["transcription"].enabled = False
        context.save_config(config)
        problem = next(p for p in await store.get_problems() if p.kind == "recording")
        assert problem.retry_available is False


async def test_a_recording_removed_during_transcription_is_reported_as_removed(store, monkeypatch):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        job = await context.accept_recording(request(audio))
        real = context.provider.transcribe

        async def transcribe_then_remove(*args, **kwargs):
            result = await real(*args, **kwargs)
            await store.remove_artifact(audio)
            return result

        monkeypatch.setattr(context.provider, "transcribe", transcribe_then_remove)
        await context.recordings.tick()
    failed = await context.recording_status({"id": job["id"]})
    assert failed["state"] == "failed" and failed["error_rule"] == "recording-missing"
    assert "removed before the note could cite it" in failed["error"]
    assert "new external id" not in failed["error"]
    assert await context.recordings.tick() is False


async def test_unmatched_recording_creates_review_note_with_capture_time(store):
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        payload = request(audio, frontmatter={})
        accepted = await context.accept_recording(payload)
        await context.recordings.tick()
    done = await context.recording_status({"id": accepted["id"]})
    assert done["state"] == "succeeded"
    note = await store.get_note(done["note_id"])
    assert note.path.startswith("Review/")
    assert str(note.frontmatter["date"]).startswith("2026-11-09T09:30")
    assert not note.frontmatter.get("meeting")


async def test_recovering_shared_recording_keeps_audio_until_its_note_is_reviewed(
    store, monkeypatch
):
    import httpx
    from coppermind_store import model_provider

    monkeypatch.setattr(model_provider, "JOB_POLL_SECONDS", 0)
    async with Service() as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        first = await context.accept_recording(request(audio))
        await context.recordings.tick()
        second = await context.accept_recording(
            request(audio, external_source_id="plaud/rec-2", metadata={"duration_seconds": 7500})
        )
        note = await store.get_note(
            (await context.recording_status({"id": first["id"]}))["note_id"]
        )
        await store.patch_frontmatter(
            note.id, PatchFrontmatter(set={"reviewed": True}), note.content_hash
        )
        assert await filing.file_reviewed(store) == {"filed": 1, "filing_failed": 0}
        rewind(
            context,
            second["id"],
            state="running",
            attempts=1,
            remote_job_id="lost-job",
            remote_job_started=datetime.now(UTC).timestamp(),
        )
        archive = store.artifacts_root / audio / "r0001" / "standup.m4a"
        requests = []

        async def handle(http_request):
            requests.append(http_request.method)
            assert archive.read_bytes() == AUDIO
            if http_request.method == "GET":
                return httpx.Response(404, json={"error": {"reason": "not_found"}})
            assert http_request.headers["idempotency-key"] == second["id"]
            assert AUDIO in await http_request.aread()
            return httpx.Response(202, json={"id": "new-job", "status": "done", "result": REPLY})

        context.provider.transport = httpx.MockTransport(handle)
        assert await context.recordings.tick() is True
        assert requests == ["GET", "POST"]
        assert archive.read_bytes() == AUDIO
        assert not context.recordings.load(first["id"]).audio_removed
        assert (await context.recording_status({"id": second["id"]}))["state"] == "succeeded"
        assert await context.recordings.tick() is False
        assert archive.read_bytes() == AUDIO


@pytest.mark.parametrize("removed", ["note", "recording"])
async def test_voice_pending_is_private_and_follows_recording_or_note_removal(store, removed):
    from coppermind_store import artifacts

    vector = {"model": "voice", "version": "v1", "vector": [0.1, 0.2]}
    speakers = [{"label": "Speaker 1", "speech_seconds": 30.0, "embeddings": [vector]}]
    async with Service(reply={**REPLY, "speakers": speakers}) as service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        accepted = await context.accept_recording(request(audio))
        await context.recordings.tick()
    job = context.recordings.load(accepted["id"])
    assert b'name="include_speaker_embeddings"\r\n\r\ntrue' in service.requests[0]["body"]
    assert context.read("voice-pending/" + job.id, []) == [
        {**speakers[0], "label": "Unknown voice 1"}
    ]
    assert "vector" not in json.dumps(context.recordings.saved_transcript(job))
    manifest = await store.get_source(job.source_id)
    source = await store.get_source_artifact(
        job.source_id, manifest.current_revision, "transcript.json"
    )
    assert "vector" not in source.content
    if removed == "note":
        await set_note_state(store, job.note_id, "deleted")
        await context.recordings.tick()
    else:
        await artifacts.remove_artifact(store, audio)
    assert context.read("voice-pending/" + job.id, None) is None


async def test_voice_pending_is_removed_for_a_deleted_note_with_a_stuck_review(store):
    vector = {"model": "voice", "version": "v1", "vector": [0.1, 0.2]}
    speakers = [{"label": "Speaker 1", "speech_seconds": 30.0, "embeddings": [vector]}]
    async with Service(reply={**REPLY, "speakers": speakers}) as service:
        context = await setup_transcription(store, service)
        accepted = await context.accept_recording(request(await store_audio(store)))
        await context.recordings.tick()
    job = context.recordings.load(accepted["id"])
    assert context.read("voice-pending/" + job.id, []) == [
        {**speakers[0], "label": "Unknown voice 1"}
    ]
    context.write("review-completions/" + job.note_id, {"state": "applying"})
    await set_note_state(store, job.note_id, "deleted")
    await context.recordings.tick()
    assert context.read("voice-pending/" + job.id, None) is None


async def test_reviewed_voice_is_suggested_for_later_recording(store):
    from coppermind.entities import EntityInput

    person = await store.entities.create(EntityInput(kind="person", name="Alex Morgan"))
    vector = {"model": "voice", "version": "v1", "vector": [1.0, 0.0]}
    speakers = [{"label": "Speaker 1", "speech_seconds": 30.0, "embeddings": [vector]}]
    async with Service(reply={**REPLY, "speakers": speakers}) as service:
        context = await setup_transcription(store, service)
        await context.setup_lifecycle({"revision": context.config().revision})
        first = await context.accept_recording(request(await store_audio(store)))
        await context.recordings.tick()
        first_job = context.recordings.load(first["id"])
        note = await store.get_note(first_job.note_id)
        detail = await context.lifecycle_detail({"note_id": note.id})
        manual = detail["manual_speaker_review"]
        await context.complete_review(
            {
                "note_id": note.id,
                "if_match": note.content_hash,
                "confirmation": "Complete review",
                "selected": [],
                "manual_source_id": manual["source_id"],
                "manual_source_revision": manual["revision"],
                "manual_speakers": {"Unknown voice 1": "person:" + person["id"]},
            }
        )
        second_audio = await store_audio(store, name="second.m4a", data=AUDIO + b"second")
        second = await context.accept_recording(request(second_audio, external_source_id="second"))
        for _ in range(3):
            if context.recordings.load(second["id"]).state == "succeeded":
                break
            await context.recordings.tick()
        second_job = context.recordings.load(second["id"])
        assert second_job.state == "succeeded"
        detail = await context.lifecycle_detail({"note_id": second_job.note_id})
        match = next(iter(detail["manual_speaker_review"]["voice_matches"].values()))[0]
        assert match["id"] == "person:" + person["id"] and match["strength"] == "strong"
        assert "vector" not in json.dumps(detail)


async def test_calendar_attendee_count_is_sent_as_maximum_speakers(store, monkeypatch):
    async with Service() as service:
        context = await setup_transcription(store, service)

        async def candidates(note):
            return [{"event_id": "event", "title": "Standup", "people": [{}, {}, {}]}]

        monkeypatch.setattr(context, "calendar_candidates", candidates)
        accepted = await context.accept_recording(request(await store_audio(store)))
        await context.recordings.tick()
        assert context.recordings.load(accepted["id"]).state == "succeeded"
        body = service.requests[0]["body"]
        assert b'name="max_speakers"\r\n\r\n3' in body
        assert b'name="num_speakers"' not in body


async def test_calendar_failure_does_not_block_transcription(store, monkeypatch):
    from coppermind_store.calendar_import import CalendarTransientError

    async with Service() as service:
        context = await setup_transcription(store, service)

        async def candidates(note):
            raise CalendarTransientError("calendar lookup timed out")

        monkeypatch.setattr(context, "calendar_candidates", candidates)
        accepted = await context.accept_recording(request(await store_audio(store)))
        await context.recordings.tick()
        assert context.recordings.load(accepted["id"]).state == "succeeded"
        body = service.requests[0]["body"]
        assert b'name="max_speakers"' not in body
        assert b'name="num_speakers"' not in body


async def test_unexpected_calendar_error_does_not_block_transcription(store, monkeypatch):
    from sqlalchemy.exc import OperationalError

    async with Service() as service:
        context = await setup_transcription(store, service)

        async def candidates(note):
            raise OperationalError("select", {}, RuntimeError("database is locked"))

        monkeypatch.setattr(context, "calendar_candidates", candidates)
        accepted = await context.accept_recording(request(await store_audio(store)))
        await context.recordings.tick()
        assert context.recordings.load(accepted["id"]).state == "succeeded"
        body = service.requests[0]["body"]
        assert b'name="max_speakers"' not in body
        assert b'name="num_speakers"' not in body


async def test_owner_speaker_count_is_sent_exactly_and_beats_the_calendar(store, monkeypatch):
    async with Service() as service:
        context = await setup_transcription(store, service)

        async def candidates(note):
            return [{"event_id": "event", "title": "Standup", "people": [{}, {}, {}]}]

        monkeypatch.setattr(context, "calendar_candidates", candidates)
        accepted = await context.accept_recording(
            request(await store_audio(store), metadata={"num_speakers": 2})
        )
        await context.recordings.tick()
        assert context.recordings.load(accepted["id"]).state == "succeeded"
        body = service.requests[0]["body"]
        assert b'name="num_speakers"\r\n\r\n2' in body
        assert b'name="max_speakers"' not in body


async def test_companion_voice_flow_through_public_api(store):
    import httpx
    from coppermind_store.auth import InternalAuth
    from coppermind_store.context_api import router as context_router
    from coppermind_store.entity_api import router as entity_router
    from fastapi import FastAPI

    from coppermind.entities import EntityInput
    from coppermind.store_client import HttpStoreClient
    from tests.integration.test_store_write_protocol import authorized_api

    alex = await store.entities.create(EntityInput(kind="person", name="Alex Morgan"))
    blair = await store.entities.create(EntityInput(kind="person", name="Blair Stone"))
    vector = {"model": "voice", "version": "v1", "vector": [1.0, 0.0]}
    speakers = [{"label": "Speaker 1", "speech_seconds": 30.0, "embeddings": [vector]}]
    internal = FastAPI()
    internal.state.store = store
    internal.state.auth = InternalAuth("synthetic-internal-token")
    internal.include_router(context_router)
    internal.include_router(entity_router)
    client = HttpStoreClient(
        "http://store", "synthetic-internal-token", transport=httpx.ASGITransport(app=internal)
    )
    app, headers = authorized_api(store)
    app.state.store = client
    async with Service(reply={**REPLY, "speakers": speakers}) as service:
        context = await setup_transcription(store, service)
        await context.setup_lifecycle({"revision": context.config().revision})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api", headers=headers
        ) as api:
            alex_url, blair_url = (f"/v1/entities/{p['id']}/voice" for p in (alex, blair))

            async def make_recording(name, content, external):
                audio = await store_audio(store, name=name, data=content)
                accepted = await context.accept_recording(
                    request(audio, external_source_id=external)
                )
                for _ in range(4):
                    job = context.recordings.load(accepted["id"])
                    if job.state == "succeeded":
                        return job
                    await context.recordings.tick()
                pytest.fail("recording was not processed")

            async def review(job, person_id, *, name_unsigned=False):
                response = await api.get(f"/v1/notes/{job.note_id}/review")
                assert response.status_code == 200, response.text
                data = response.json()
                manual = data["manual_speaker_review"]
                choices = {
                    next(
                        label for label in manual["speakers"] if label.startswith("Unknown voice ")
                    ): "person:" + person_id
                }
                if name_unsigned:
                    assert manual["voice_unavailable"] == ["Speaker 2"]
                    choices["Speaker 2"] = "person:" + blair["id"]
                completed = await api.post(
                    f"/v1/notes/{job.note_id}/review",
                    json={
                        "if_match": data["note"]["content_hash"],
                        "confirmation": "Complete review",
                        "selected": [],
                        "manual_source_id": manual["source_id"],
                        "manual_source_revision": manual["revision"],
                        "manual_speakers": choices,
                    },
                )
                assert completed.status_code == 200, completed.text
                assert "vector" not in response.text and "vector" not in completed.text
                return data

            first = await make_recording("one.m4a", AUDIO, "voice-http-1")
            earlier = await make_recording("earlier.m4a", AUDIO + b"earlier", "voice-http-earlier")
            unknowns = (await api.get("/v1/entities/voice/unknowns")).json()
            assert len(unknowns) == 1
            unknown = next(voice for voice in unknowns.values() if len(voice["assignments"]) == 2)
            assert len(unknown["assignments"]) == 2
            earlier_detail = (await api.get(f"/v1/notes/{earlier.note_id}/review")).json()
            completed = await api.post(
                f"/v1/notes/{earlier.note_id}/review",
                json={
                    "if_match": earlier_detail["note"]["content_hash"],
                    "confirmation": "Complete review",
                    "selected": [],
                },
            )
            assert completed.status_code == 200, completed.text
            from coppermind.store_protocol import ReplaceNote

            note = await store.get_note(earlier.note_id)
            label = unknown["name"]
            original = f"{label}: sent plan.\n{label}: original."
            accepted = f"{label}: sent plan.\n{label}: owner edited."
            body = f"Owner wrote {label}.\n\n## Summary\n\n{accepted}\n"
            await store.replace_note(
                note.id,
                ReplaceNote(frontmatter=note.frontmatter, body=body),
                if_match=note.content_hash,
            )
            receipt = context.read("review-completions/" + earlier.note_id, {})
            receipt["summary"] = {
                "generated": {"summary": original, "decisions": "", "action_items": ""},
                "value": {"summary": accepted, "decisions": "", "action_items": ""},
                "body": body,
            }
            receipt["summary_applied"] = True
            context.write("review-completions/" + earlier.note_id, receipt)
            await review(first, alex["id"], name_unsigned=True)
            changed = await store.get_note(earlier.note_id)
            assert f"Owner wrote {label}." in changed.body
            assert "Alex Morgan: sent plan." in changed.body
            assert f"{label}: owner edited." in changed.body
            manifest = await store.get_source(earlier.source_id)
            transcript = await store.get_source_artifact(
                earlier.source_id, manifest.current_revision, "transcript.json"
            )
            assert '"speaker": "Alex Morgan"' in transcript.content
            assert '"speaker": "Speaker 2"' in transcript.content
            assert (await api.get(blair_url)).json()["signatures"] == []
            alex_voice = (await api.get(alex_url)).json()
            assert len(alex_voice["signatures"]) == 2
            appearance = next(
                a for a in alex_voice["assignments"] if a["note_id"] == earlier.note_id
            )
            from coppermind.entities import EntityChange

            await store.entities.refresh()
            current = await store.entities.get(alex["id"])
            await store.entities.change(
                alex["id"],
                EntityChange(version=current["version"], action="rename", name="Alex New"),
            )
            separated = await api.post(
                f"/v1/entities/voice/voices/person:{alex['id']}/split",
                json={"note_id": earlier.note_id, "label": appearance["label"]},
            )
            assert separated.status_code == 200, separated.text
            separated_label = (await api.get("/v1/entities/voice/unknowns")).json()[
                separated.json()["id"]
            ]["name"]
            separated_note = await store.get_note(earlier.note_id)
            assert f"{separated_label}: sent plan." in separated_note.body
            assert f"Owner wrote {label}." in separated_note.body
            assert f"{label}: owner edited." in separated_note.body
            manifest = await store.get_source(earlier.source_id)
            transcript = await store.get_source_artifact(
                earlier.source_id, manifest.current_revision, "transcript.json"
            )
            assert '"speaker": "Unknown voice ' in transcript.content
            assert len((await api.get(alex_url)).json()["signatures"]) == 1
            second = await make_recording("two.m4a", AUDIO + b"2", "voice-http-2")
            detail = (await api.get(f"/v1/notes/{second.note_id}/review")).json()
            assert (
                detail["manual_speaker_review"]["voice_matches"][
                    next(
                        label
                        for label in detail["manual_speaker_review"]["speakers"]
                        if label.startswith("Unknown voice ")
                    )
                ][0]["id"]
                == "person:" + alex["id"]
            )
            await review(second, blair["id"])
            assert len((await api.get(alex_url)).json()["signatures"]) == 1
            assert len((await api.get(blair_url)).json()["signatures"]) == 2
            attached = await api.post(
                f"/v1/entities/voice/unknowns/{separated.json()['id']}/combine",
                json={"target": "person:" + alex["id"]},
            )
            assert attached.status_code == 200, attached.text
            manifest = await store.get_source(earlier.source_id)
            transcript = await store.get_source_artifact(
                earlier.source_id, manifest.current_revision, "transcript.json"
            )
            assert '"speaker": "Alex New"' in transcript.content
            assert (await api.delete(alex_url)).json()["signatures"] == []
            assert (
                await api.put("/v1/entities/voice/recognition", json={"enabled": False})
            ).json()["enabled"] is False
            before = (await api.get("/v1/entities/voice/unknowns")).json()
            await make_recording("off.m4a", AUDIO + b"off", "voice-http-off")
            assert (await api.get("/v1/entities/voice/unknowns")).json() == before


async def test_cascade_preserves_a_failed_provisional_recording_for_retry(store, monkeypatch):
    from coppermind_store.voice_store import VoiceStore

    from coppermind.entities import EntityInput

    person = await store.entities.create(EntityInput(kind="person", name="Alex Morgan"))
    vector = {"model": "voice", "version": "v1", "vector": [1.0, 0.0]}
    speakers = [{"label": "Speaker 1", "speech_seconds": 30.0, "embeddings": [vector]}]
    async with Service(reply={**REPLY, "speakers": speakers}) as service:
        context = await setup_transcription(store, service)
        await context.setup_lifecycle({"revision": context.config().revision})
        first = await context.accept_recording(request(await store_audio(store)))
        await context.recordings.tick()
        first_job = context.recordings.load(first["id"])
        second = await context.accept_recording(
            request(
                await store_audio(store, name="second.m4a", data=AUDIO + b"2"),
                external_source_id="retry-second",
            )
        )
        second_job = context.recordings.load(second["id"])
        context.write("voice-pending/" + second_job.id, speakers)
        original_ingest = store.ingest

        async def failed_ingest(*args, **kwargs):
            raise RuntimeError("simulated filing failure")

        monkeypatch.setattr(store, "ingest", failed_ingest)
        with pytest.raises(RuntimeError, match="simulated filing failure"):
            await context.recordings.file_note(second_job, REPLY)
        monkeypatch.setattr(store, "ingest", original_ingest)
        provisional = "recording:" + second_job.id
        voice = VoiceStore(context)
        unknown_id = next(iter(voice.unknown_summaries()))
        assert any(a["note_id"] == provisional for a in voice.assignments(unknown_id))
        detail = await context.lifecycle_detail({"note_id": first_job.note_id})
        manual = detail["manual_speaker_review"]
        await context.complete_review(
            {
                "note_id": first_job.note_id,
                "if_match": detail["note"]["content_hash"],
                "confirmation": "Complete review",
                "selected": [],
                "manual_source_id": manual["source_id"],
                "manual_source_revision": manual["revision"],
                "manual_speakers": {"Unknown voice 1": "person:" + person["id"]},
            }
        )
        assert any(a["note_id"] == provisional for a in voice.assignments("person:" + person["id"]))
        await context.recordings.file_note(second_job, REPLY)
        manifest = await store.get_source(second_job.source_id)
        transcript = await store.get_source_artifact(
            second_job.source_id, manifest.current_revision, "transcript.json"
        )
        assert '"speaker": "Alex Morgan"' in transcript.content
        assert any(
            a["note_id"] == second_job.note_id for a in voice.assignments("person:" + person["id"])
        )


async def test_combine_two_unknown_voices_through_public_api(store):
    import httpx
    from coppermind_store.auth import InternalAuth
    from coppermind_store.entity_api import router as entity_router
    from fastapi import FastAPI

    from coppermind.store_client import HttpStoreClient
    from tests.integration.test_store_write_protocol import authorized_api

    def reply(vector):
        return {
            **REPLY,
            "speakers": [
                {
                    "label": "Speaker 1",
                    "speech_seconds": 30,
                    "embeddings": [{"model": "m", "version": "1", "vector": vector}],
                }
            ],
        }

    internal = FastAPI()
    internal.state.store = store
    internal.state.auth = InternalAuth("synthetic-internal-token")
    internal.include_router(entity_router)
    client = HttpStoreClient(
        "http://store", "synthetic-internal-token", transport=httpx.ASGITransport(app=internal)
    )
    app, headers = authorized_api(store)
    app.state.store = client
    async with Service(reply=reply([1, 0])) as service:
        context = await setup_transcription(store, service)
        await context.accept_recording(request(await store_audio(store)))
        await context.recordings.tick()
        service.reply = reply([0, 1])
        second = await context.accept_recording(
            request(
                await store_audio(store, name="b.m4a", data=AUDIO + b"b"),
                external_source_id="other-voice",
            )
        )
        await context.recordings.tick()
        job = context.recordings.load(second["id"])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api", headers=headers
        ) as api:
            unknowns = (await api.get("/v1/entities/voice/unknowns")).json()
            assert len(unknowns) == 2
            identities = list(unknowns)
            source = next(
                k for k, v in unknowns.items() if v["assignments"][0]["note_id"] == job.note_id
            )
            target = next(k for k in identities if k != source)
            result = await api.post(
                f"/v1/entities/voice/unknowns/{source}/combine", json={"target": target}
            )
            assert result.status_code == 200, result.text
            unknowns = (await api.get("/v1/entities/voice/unknowns")).json()
            assert list(unknowns) == [target]
            assert len(unknowns[target]["assignments"]) == 2
            manifest = await store.get_source(job.source_id)
            transcript = await store.get_source_artifact(
                job.source_id, manifest.current_revision, "transcript.json"
            )
            assert unknowns[target]["name"] in transcript.content


@contextlib.asynccontextmanager
async def two_unknown_voices(store):
    def reply(vector):
        return {
            **REPLY,
            "speakers": [
                {
                    "label": "Speaker 1",
                    "speech_seconds": 30,
                    "embeddings": [{"model": "m", "version": "1", "vector": vector}],
                }
            ],
        }

    from coppermind_store.voice_store import VoiceStore

    async with Service(reply=reply([1, 0])) as service:
        context = await setup_transcription(store, service)
        await context.accept_recording(request(await store_audio(store)))
        await context.recordings.tick()
        service.reply = reply([0, 1])
        second = await context.accept_recording(
            request(
                await store_audio(store, name="b.m4a", data=AUDIO + b"b"),
                external_source_id="other-voice",
            )
        )
        await context.recordings.tick()
        job = context.recordings.load(second["id"])
        voice = VoiceStore(context)
        unknowns = voice.unknown_summaries()
        source = next(
            k for k, v in unknowns.items() if v["assignments"][0]["note_id"] == job.note_id
        )
        target = next(k for k in unknowns if k != source)
        yield context, job, voice, source, target


async def transcript_of(store, job):
    manifest = await store.get_source(job.source_id)
    artifact = await store.get_source_artifact(
        job.source_id, manifest.current_revision, "transcript.json"
    )
    return manifest.current_revision, artifact.content


async def test_combine_moves_the_voice_now_and_renames_an_unfinished_review_when_it_ends(
    store, monkeypatch
):
    from coppermind_store import voice_relabel

    async with two_unknown_voices(store) as (context, job, voice, source, target):
        name = voice.unknown_summaries()[target]["name"]
        revision, before = await transcript_of(store, job)
        context.write("review-completions/" + job.note_id, {"state": "applying"})

        await voice_relabel.combine(context, source, target)

        assert list(voice.unknown_summaries()) == [target]
        assert len(voice.assignments(target)) == 2
        assert await transcript_of(store, job) == (revision, before)
        assert [d["note_id"] for d in voice.deferred_relabels()] == [job.note_id]

        await context.recordings.tick()
        assert await transcript_of(store, job) == (revision, before)
        assert len(voice.deferred_relabels()) == 1

        context.write("review-completions/" + job.note_id, {"state": "complete"})
        relabel = voice_relabel.relabel_recording

        async def crash(*args):
            raise RuntimeError("stopped before the note was rewritten")

        monkeypatch.setattr(voice_relabel, "relabel_recording", crash)
        await context.recordings.tick()
        assert len(voice.deferred_relabels()) == 1
        assert await transcript_of(store, job) == (revision, before)

        monkeypatch.setattr(voice_relabel, "relabel_recording", relabel)
        await context.recordings.tick()
        assert voice.deferred_relabels() == []
        new_revision, after = await transcript_of(store, job)
        assert new_revision == revision + 1
        assert name in after
        await context.recordings.tick()
        assert (await transcript_of(store, job))[0] == new_revision


async def test_combining_a_stale_voice_defers_an_unfinished_review_from_an_earlier_combine(store):
    from coppermind_store.voice_relabel import apply_deferred, combine

    async with two_unknown_voices(store) as (context, job, voice, source, target):
        await combine(context, source, target)
        revision, before = await transcript_of(store, job)
        context.write("review-completions/" + job.note_id, {"state": "applying"})

        await combine(context, source, target)

        assert list(voice.unknown_summaries()) == [target]
        assert len(voice.assignments(target)) == 2
        assert [d["note_id"] for d in voice.deferred_relabels()] == [job.note_id]
        assert await transcript_of(store, job) == (revision, before)
        assert not await apply_deferred(context)

        context.write("review-completions/" + job.note_id, {"state": "complete"})
        assert await apply_deferred(context)
        assert voice.deferred_relabels() == []


async def test_a_voice_cannot_be_separated_from_a_note_whose_review_is_unfinished(store):
    from coppermind_store.voice_relabel import separate

    async with two_unknown_voices(store) as (context, job, voice, source, target):
        assignment = voice.assignments(source)[0]
        revision, before = await transcript_of(store, job)
        context.write("review-completions/" + job.note_id, {"state": "applying"})

        with pytest.raises(ValueError, match="Finish the review of"):
            await separate(context, source, assignment["note_id"], assignment["label"])

        assert voice.assignments(source) == [assignment]
        assert await transcript_of(store, job) == (revision, before)


async def test_companion_retry_permissions_and_waiting_count_through_real_store(store):
    """A refused retry keeps the failure; an accepted retry removes it from the count."""
    import httpx
    from coppermind_store.auth import InternalAuth
    from coppermind_store.context_api import router as context_router
    from fastapi import FastAPI

    from coppermind.api_keys import create_key, load_keys
    from coppermind.store_client import HttpStoreClient
    from tests.integration.test_store_write_protocol import authorized_api

    internal = FastAPI()
    internal.state.store = store
    internal.state.auth = InternalAuth("synthetic-internal-token")
    internal.include_router(context_router)
    client = HttpStoreClient(
        "http://store", "synthetic-internal-token", transport=httpx.ASGITransport(app=internal)
    )
    app, _ = authorized_api(store)
    app.state.store = client
    current = load_keys(store.control.store)
    reader, reader_key = create_key("reader", ["notes:read", "sources:read"])
    # Use precisely the permission names shown in settings to provision the key.
    import subprocess
    from pathlib import Path

    driver = (
        Path(__file__).resolve().parents[2]
        / "services/admin/tests/companion-permission-requests.cjs"
    )
    captured = json.loads(subprocess.check_output(["node", str(driver)], text=True))
    # The ticks the settings name are Admin's own, so the two cannot drift apart.
    from coppermind.api_keys import COMPANION_PERMISSIONS, PERMISSIONS, scopes_for

    ticks = {label: key for key, label, _ in PERMISSIONS}
    description = next(text for text in captured["descriptions"] if "create a new key" in text)
    advertised = description.split("create a new key with: ", 1)[1].split(". Select", 1)[0]
    names = [part.removeprefix("and ") for part in advertised.split(", ")]
    assert {ticks[name] for name in names} == set(COMPANION_PERMISSIONS)
    companion, companion_key = create_key("companion", scopes_for(ticks[name] for name in names))
    current.keys.extend([reader, companion])
    store.control.store.write("keys", current.model_dump(mode="json"), if_revision=current.revision)
    async with Service() as service:
        context = await setup_transcription(store, service)
        accepted = await context.accept_recording(request(await store_audio(store)))
        job = context.recordings.load(accepted["id"])
        job.state = "failed"
        job.attempts = job.attempt_limit
        job.set_error("synthetic failure", "job-error")
        context.recordings.save(job)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as api:
            reader_headers = {"Authorization": f"Bearer {reader_key}"}
            before = await api.get("/v1/recordings/waiting", headers=reader_headers)
            assert before.status_code == 200
            assert len(before.json()["failed"]) == 1
            denied = await api.post(f"/v1/recordings/{job.id}/retry", headers=reader_headers)
            assert denied.status_code == 403
            assert (
                len(
                    (await api.get("/v1/recordings/waiting", headers=reader_headers)).json()[
                        "failed"
                    ]
                )
                == 1
            )
            # Replay actual view startup calls through the public authorization stack.
            headers = {"Authorization": f"Bearer {companion_key}"}
            for call in captured["calls"]:
                response = await api.request(call["method"], call["path"], headers=headers)
                assert response.status_code != 403, (call, response.text)
            recognition = await api.put(
                "/v1/entities/voice/recognition", headers=headers, json={"enabled": False}
            )
            assert recognition.status_code != 403
            assert (await api.get("/v1/recordings/waiting", headers=headers)).status_code == 200
            accepted_retry = await api.post(f"/v1/recordings/{job.id}/retry", headers=headers)
            assert accepted_retry.status_code == 200
            after = await api.get("/v1/recordings/waiting", headers=reader_headers)
            assert after.status_code == 200 and after.json()["failed"] == []
            assert context.recordings.load(job.id).state == "queued"
    await client.aclose()


async def test_rejected_recording_exposes_structured_field_reason(store):
    async with Service() as service:
        context = await setup_transcription(store, service)
        accepted = await context.accept_recording(
            request(await store_audio(store), frontmatter={"date": "2026-11-09", "account": 42})
        )
        await context.recordings.tick()
        job = context.recordings.load(accepted["id"])
        assert job.error_rule == "note-rejected"
        assert "account: expected a list" in job.error
        row = (await context.recording_waiting({}))["failed"][0]
        assert row["field_problems"] == [{"field": "account", "reason": "should be a list"}]
        assert "external id" not in str(row) and "schema" not in str(row)
        # Earlier versions saved the input but had no field_problems property.
        path = context.root / "recordings" / f"{job.id}.json"
        saved = json.loads(path.read_text())
        del saved["field_problems"]
        path.write_text(json.dumps(saved))
        row = (await context.recording_waiting({}))["failed"][0]
        assert row["field_problems"] == [{"field": "account", "reason": "should be a list"}]
        assert row["field_problems_unavailable"] is False
        saved["frontmatter"]["account"] = ["valid"]
        path.write_text(json.dumps(saved))
        row = (await context.recording_waiting({}))["failed"][0]
        assert row["field_problems"] == []
        assert row["field_problems_unavailable"] is True
