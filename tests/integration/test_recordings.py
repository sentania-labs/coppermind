"""Audio-only intake against PostgreSQL and real files, with a slow stand-in transcriber.

No transcription service is reachable from the test environment, so the service here is a
local stand-in (`services.store.tests.test_transcription_call.Service`) that returns the
documented `diarized_json` shape after a delay, over a real socket.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from coppermind_store import staged
from coppermind_store.recordings import render_transcript

from coppermind.context_config import FeatureAssignment
from coppermind.store_protocol import (
    CreateArtifact,
    StagedReservation,
    ValidationFailed,
)
from services.store.tests.test_transcription_call import REPLY, Service
from tests.integration.test_review_filing import drain

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

    # The transcript is the note's source, speakers as the service gave them.
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

    # The audio is still stored: nothing here deletes it.
    kept = await store.get_artifact_bytes(audio)
    assert await drain(kept) == AUDIO


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


async def test_nothing_is_accepted_without_a_transcription_service_or_a_recording(store):
    async with Service() as service:
        context = store.context
        audio = await store_audio(store)
        with pytest.raises(ValueError, match="No transcription service is set up"):
            await context.accept_recording(request(audio))
        context = await setup_transcription(store, service)
        with pytest.raises(ValueError, match="no stored file"):
            await context.accept_recording(request("01ARZ3NDEKTSV4RRFFQ69G5FAV"))
        note_file = await store.create_artifact(
            CreateArtifact(kind="artifact", filename="notes.txt", content_base64="aGk=")
        )
        with pytest.raises(ValueError, match="not a recording"):
            await context.accept_recording(request(note_file.id))
        video = await store_audio(store, name="clip.mp4", data=AUDIO + b"v", mime="video/mp4")
        with pytest.raises(ValueError, match="video/mp4, not a recording"):
            await context.accept_recording(request(video))


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
                raise ValidationFailed(["frontmatter: type is required"])
            return await real(ingest_request, **kwargs)

        store.ingest = refuse
        await context.recordings.tick()
        failed = await context.recording_status({"id": job["id"]})
        assert failed["error_rule"] == "note-rejected" and "type is required" in failed["error"]
        assert "transcript is kept" in failed["error"]
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
        # A new request still needs the service.
        with pytest.raises(ValueError, match="No transcription service is set up"):
            await context.accept_recording(request(audio, external_source_id="plaud/rec-2"))


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
