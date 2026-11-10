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


async def setup_transcription(store, service, *, enabled=True, timeout_s=30, **extra):
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
        timeout_s=timeout_s,
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
    assert kept.content == AUDIO


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


async def test_a_refusal_lands_in_problems_keeps_the_audio_and_retries_on_request(store):
    service = Service(status=404, reply={"error": "nope"})
    async with service:
        context = await setup_transcription(store, service)
        audio = await store_audio(store)
        job = await context.accept_recording(request(audio))
        await context.recordings.tick()
        failed = await context.recording_status({"id": job["id"]})
        assert failed["state"] == "failed" and failed["error_rule"] == "transcription-http-404"
        assert "no transcription endpoint" in failed["error"]
        # A refusal the same request would only repeat is not tried again by itself.
        assert await context.recordings.tick() is False
        assert len(service.requests) == 1

        problems = [p for p in await store.get_problems() if p.kind == "recording"]
        assert len(problems) == 1 and "no transcription endpoint" in problems[0].reason
        assert problems[0].reference == "Standup"
        assert (await store.get_artifact_bytes(audio)).content == AUDIO

        listed = (await context.snapshot())["recordings"]
        assert listed[0]["state"] == "failed" and listed[0]["can_retry"] is True

        service.status, service.reply = 200, REPLY
        await context.retry_recording({"id": job["id"]})
        assert (await context.recording_status({"id": job["id"]}))["state"] == "queued"
        await context.recordings.tick()
    done = await context.recording_status({"id": job["id"]})
    assert done["state"] == "succeeded" and done["note_id"]
    assert not [p for p in await store.get_problems() if p.kind == "recording"]


async def test_a_silent_service_backs_off_before_the_next_attempt(store):
    async with Service(delay=6) as service:
        context = await setup_transcription(store, service, timeout_s=5)
        audio = await store_audio(store)
        job = await context.accept_recording(request(audio))
        await context.recordings.tick()
    failed = await context.recording_status({"id": job["id"]})
    assert failed["error_rule"] == "provider-no-response"
    assert datetime.fromisoformat(failed["retry_after"]) > datetime.now(UTC) + timedelta(minutes=4)
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
