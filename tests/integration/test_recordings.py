"""Audio-only intake against PostgreSQL and real files, with a slow stand-in transcriber.

No transcription service is reachable from the test environment, so the service here is a
local stand-in (`services.store.tests.test_transcription_call.Service`) that returns the
documented `diarized_json` shape after a delay, over a real socket.
"""

from __future__ import annotations

import asyncio
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
