"""Speaker review against the real store and a local stand-in model."""

import asyncio
import json
from typing import Any

import httpx
import pytest
from coppermind_store.model_provider import ModelProvider
from coppermind_store.recordings import TranscriptionJob, render_transcript

from coppermind.entities import EntityInput
from coppermind.store_protocol import (
    IngestArtifact,
    IngestNote,
    IngestRequest,
    IngestSource,
    PatchFrontmatter,
)
from tests.integration.test_context_enrichment import configure


@pytest.fixture
async def standin():
    """Serve OpenAI-compatible responses on loopback for the real model client."""
    service: dict[str, Any] = {"handler": None}

    async def handle(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        length = next(
            int(line.split(b":", 1)[1])
            for line in headers.split(b"\r\n")
            if line.lower().startswith(b"content-length:")
        )
        body = await reader.readexactly(length)
        response = service["handler"](
            httpx.Request("POST", "http://localhost/v1/chat/completions", content=body)
        )
        data = response.content
        writer.write(
            (
                f"HTTP/1.1 {response.status_code} OK\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n"
            ).encode()
            + data
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    service["url"] = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"
    try:
        yield service
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "choice",
    [
        "corrected",
        "unknown",
        "unselected",
        "failed",
        "auto_link",
        "projection_interrupted",
        "second_failed",
    ],
)
async def test_review_corrects_a_speaker_and_replays_without_another_revision(
    store, monkeypatch, choice, standin
):
    context = await configure(store)
    await context.setup_lifecycle({"revision": context.config().revision})
    transcript = {
        "segments": [
            {"start": 0, "speaker": "Speaker 1", "text": "Hello Casey."},
            {"start": 3, "speaker": "Speaker 2", "text": "Hello."},
            {"start": 6, "speaker": "Speaker 1", "text": "Alex sent the notes."},
        ]
    }
    result = await store.ingest(
        IngestRequest(
            source=IngestSource(
                provider="test-audio",
                external_source_id="speaker-review",
                source_type="transcript",
                artifacts=[
                    IngestArtifact(
                        name="transcript.txt",
                        mime_type="text/plain",
                        content=render_transcript(transcript),
                    ),
                    IngestArtifact(
                        name="transcript.json",
                        mime_type="application/json",
                        content=json.dumps(transcript),
                    ),
                ],
            ),
            note=IngestNote(title="Planning", frontmatter={"date": "2026-10-14T09:00:00"}),
        )
    )
    context.recordings.save(
        TranscriptionJob(
            id="a" * 64,
            artifact_id="b" * 64,
            filename="meeting.wav",
            mime_type="audio/wav",
            size_bytes=1,
            title="Planning",
            provider="test-audio",
            external_source_id="speaker-review",
            state="succeeded",
            transcribed=True,
            created_at="2026-10-14T14:00:00+00:00",
            note_id=result.note.id,
            note_path=result.note.path,
            source_id=result.source.id,
        )
    )
    note = await store.get_note(result.note.id)
    event = (await context.calendar_candidates(note))[0]
    if choice not in {"auto_link", "second_failed"}:
        note = await store.patch_frontmatter(
            note.id, PatchFrontmatter(set={"calendar_event": event["event_id"]}), note.content_hash
        )
    person = await store.entities.create(
        EntityInput(kind="person", name="Alex Morgan", emails=["alex@example.test"])
    )
    observed = []

    def model(request):
        evidence = json.loads(json.loads(request.content)["messages"][-1]["content"])
        if "transcript" in evidence:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {"summary": "Greetings.", "decisions": "", "action_items": ""}
                                )
                            },
                        }
                    ]
                },
            )
        observed.append(evidence)
        if choice == "failed" or (choice == "second_failed" and len(observed) > 1):
            return httpx.Response(503, json={"error": "unavailable"})
        if choice in {"auto_link", "second_failed"} and not evidence["note"]["frontmatter"].get(
            "calendar_event"
        ):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {
                                        "suggestions": [
                                            {
                                                "kind": "event",
                                                "event_ids": [event["event_id"]],
                                                "confidence": 0.99,
                                                "reason": "Time and title match.",
                                            }
                                        ],
                                        "unresolved": [],
                                    }
                                )
                            },
                        }
                    ]
                },
            )
        candidate = evidence["speaker_review"]["candidates"][0]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "suggestions": [
                                        {
                                            "kind": "speaker",
                                            "field": "Speaker 2",
                                            "value": candidate["id"],
                                            "confidence": 0.88,
                                            "reason": "Addressed as Casey.",
                                        }
                                    ],
                                    "unresolved": [],
                                }
                            )
                        },
                    }
                ]
            },
        )

    standin["handler"] = model
    config = context.config()
    connection = next(
        c for c in config.connections if c.id == config.assignments["enrichment"].connection
    )
    connection.base_url = standin["url"]
    context.save_config(config)
    context.provider = ModelProvider()
    job_id = await context.queue_note(note.id)
    job = context.load_job(job_id)
    await context.run_job(job)
    if choice == "second_failed":
        from coppermind_store.speaker_names import REFRESH_NOTICE

        assert job.state == "suggested", job.error
        assert len(observed) == 2
        assert (await store.get_note(note.id)).frontmatter["calendar_event"] == event["event_id"]
        detail = await context.lifecycle_detail({"note_id": note.id})
        assert detail["speaker_notice"] == REFRESH_NOTICE
        assert detail["state"] == "suggested"
        assert [s["field"] for s in detail["suggestions"] if s["kind"] == "speaker"] == [
            "Speaker 1",
            "Speaker 2",
        ]
        problems = await context.attention({})
        assert not any(item["id"] == job_id for item in problems["failed_jobs"]["items"])
        assert (await store.get_source(result.source.id)).current_revision == 1
        return
    if choice == "failed":
        assert job.state == "failed"
        problems = await context.attention({})
        assert any(item["id"] == job_id for item in problems["failed_jobs"]["items"])
        assert (await store.get_source(result.source.id)).current_revision == 1
        assert (await store.get_note(note.id)).frontmatter["reviewed"] is False
        return
    assert job.state == "suggested", job.error
    if choice == "auto_link":
        note = await store.get_note(note.id)
        assert note.frontmatter["calendar_event"] == event["event_id"]
        assert any(
            c["name"] == "Casey"
            for s in (await context.lifecycle_detail({"note_id": note.id}))["suggestions"]
            if s["kind"] == "speaker"
            for c in s["candidates"]
        )
        assert len(observed) == 2
    detail = await context.lifecycle_detail({"note_id": note.id})
    suggestion = next(s for s in detail["suggestions"] if s["field"] == "Speaker 2")
    assert suggestion["display_value"] == "Casey"
    assert observed[0]["speaker_review"]["excerpts"] == render_transcript(transcript).splitlines()
    payload = {
        "note_id": note.id,
        "if_match": note.content_hash,
        "job_id": job_id,
        "selected": [suggestion["index"]] if choice != "unselected" else [],
        "speaker_choices": {
            str(suggestion["index"]): "person:" + person["id"]
            if choice in {"corrected", "auto_link", "projection_interrupted"}
            else ""
        },
        "confirmation": "Complete review",
    }
    if choice in {"corrected", "auto_link"}:
        patch = store.patch_frontmatter

        async def interrupted(*args, **kwargs):
            raise RuntimeError("simulated interruption")

        monkeypatch.setattr(store, "patch_frontmatter", interrupted)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            await context.complete_review(payload)
        assert (await store.get_source(result.source.id)).current_revision == 2
        monkeypatch.setattr(store, "patch_frontmatter", patch)
    if choice == "projection_interrupted":
        from coppermind_store import sources

        from coppermind.store_protocol import SourcesFilesystemUnavailable

        write_projection = sources.write_projection

        def broken_projection(*args, **kwargs):
            raise OSError("simulated projection failure")

        monkeypatch.setattr(sources, "write_projection", broken_projection)
        with pytest.raises(SourcesFilesystemUnavailable):
            await context.complete_review(payload)
        monkeypatch.setattr(sources, "write_projection", write_projection)
    completion = await context.complete_review(payload)
    assert completion["state"] == "complete"
    assert await context.complete_review(payload) == completion
    source = await store.get_source(result.source.id)
    reviewed = await store.get_note(note.id)
    assert reviewed.frontmatter["reviewed"] is True
    if choice in {"corrected", "auto_link", "projection_interrupted"}:
        assert source.current_revision == 2
        text = (await store.get_source_artifact(result.source.id, 2, "transcript.txt")).content
        assert "Speaker 1: Hello Casey." in text and "Alex Morgan: Hello." in text
        assert (
            "Alex Morgan: Hello."
            in (await context.recordings.detail({"id": "a" * 64}))["transcript"]
        )
        assert "Alex Morgan: Hello." in (store.notes_root / source.projection_path).read_text()
        assert "alex@example.test" in reviewed.frontmatter["attendees"]
        assert "[[" + person["path"].removesuffix(".md") + "]]" in reviewed.frontmatter["attendees"]
        assert len(context.read("dossiers", {})) == 1
    else:
        assert source.current_revision == 1
        assert not reviewed.frontmatter.get("attendees")
        assert not context.read("dossiers", {})


async def recorded(
    store, standin, transcript, answer, *, max_input_chars=None, attendees_field=None, summary=None
):
    """A linked recording note in Review whose model replies with `answer(evidence)`."""
    context = await configure(store)
    await context.setup_lifecycle({"revision": context.config().revision})
    result = await store.ingest(
        IngestRequest(
            source=IngestSource(
                provider="test-audio",
                external_source_id="speaker-review",
                source_type="transcript",
                artifacts=[
                    IngestArtifact(
                        name="transcript.txt",
                        mime_type="text/plain",
                        content=render_transcript(transcript),
                    ),
                    IngestArtifact(
                        name="transcript.json",
                        mime_type="application/json",
                        content=json.dumps(transcript),
                    ),
                ],
            ),
            note=IngestNote(
                title="Planning",
                body="Casey attended the meeting.",
                frontmatter={"date": "2026-10-14T09:00:00"},
            ),
        )
    )
    context.recordings.save(
        TranscriptionJob(
            id="a" * 64,
            artifact_id="b" * 64,
            filename="meeting.wav",
            mime_type="audio/wav",
            size_bytes=1,
            title="Planning",
            provider="test-audio",
            external_source_id="speaker-review",
            state="succeeded",
            transcribed=True,
            created_at="2026-10-14T14:00:00+00:00",
            note_id=result.note.id,
            note_path=result.note.path,
            source_id=result.source.id,
        )
    )
    note = await store.get_note(result.note.id)
    event = (await context.calendar_candidates(note))[0]
    note = await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"calendar_event": event["event_id"]}), note.content_hash
    )
    observed: list[dict[str, Any]] = []

    def model(request):
        evidence = json.loads(json.loads(request.content)["messages"][-1]["content"])
        if "transcript" in evidence:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    summary
                                    or {
                                        "summary": "Greetings.",
                                        "decisions": "",
                                        "action_items": "",
                                    }
                                )
                            },
                        }
                    ]
                },
            )
        observed.append(evidence)
        content = json.dumps({"suggestions": answer(evidence, event), "unresolved": []})
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        )

    standin["handler"] = model
    config = context.config()
    next(
        c for c in config.connections if c.id == config.assignments["enrichment"].connection
    ).base_url = standin["url"]
    if max_input_chars:
        config.assignments["enrichment"].max_input_chars = max_input_chars
    if attendees_field is not None:
        config.assignments["enrichment"].attendees_field = attendees_field
        config.assignments["enrichment"].contacts_field = ""
    context.save_config(config)
    context.provider = ModelProvider()
    job_id = await context.queue_note(note.id)
    await context.run_job(context.load_job(job_id))
    return context, result, note, job_id, observed


def speaker_answer(person_id, label="Speaker 2"):
    def answer(evidence, event):
        candidate = person_id(evidence)
        return [
            {
                "kind": "speaker",
                "field": label,
                "value": candidate,
                "confidence": 0.9,
                "reason": "Addressed by name.",
            }
        ]

    return answer


def alex_id(evidence):
    return next(
        c["id"] for c in evidence["speaker_review"]["candidates"] if c["name"] == "Alex Morgan"
    )


SHORT = {
    "segments": [
        {"start": 0, "speaker": "Speaker 1", "text": "Thanks, Alex."},
        {"start": 3, "speaker": "Speaker 2", "text": "Hello."},
    ]
}


async def completion(context, store, result, job_id, **choices):
    note = await store.get_note(result.note.id)
    detail = await context.lifecycle_detail({"note_id": note.id})
    index = next(s["index"] for s in detail["suggestions"] if s["field"] == "Speaker 2")
    return {
        "note_id": note.id,
        "if_match": note.content_hash,
        "job_id": job_id,
        "selected": [index],
        "speaker_choices": {str(index): choices.get("person", "")} if "person" in choices else {},
        "confirmation": "Complete review",
    }


async def test_unrelated_and_renamed_people_do_not_invalidate_a_waiting_review(store, standin):
    from coppermind.entities import EntityChange

    alex = await store.entities.create(EntityInput(kind="person", name="Alex Morgan"))
    bystander = await store.entities.create(EntityInput(kind="person", name="Blair Stone"))
    context, result, note, job_id, observed = await recorded(
        store, standin, SHORT, speaker_answer(alex_id)
    )
    assert len(observed) == 1
    await store.entities.create(EntityInput(kind="person", name="Dana Fox"))
    await store.entities.change(
        bystander["id"], EntityChange(version=bystander["version"], action="rename", name="Blair S")
    )
    await store.entities.change(
        alex["id"], EntityChange(version=alex["version"], action="rename", name="Alexandra Morgan")
    )
    completed = await context.complete_review(await completion(context, store, result, job_id))
    assert completed["state"] == "complete"
    assert len(observed) == 1
    text = (await store.get_source_artifact(result.source.id, 2, "transcript.txt")).content
    assert "Alexandra Morgan: Hello." in text and "Alex Morgan" not in text


async def test_speakers_are_renamed_when_no_attendees_property_is_set_up(store, standin):
    alex = await store.entities.create(EntityInput(kind="person", name="Alex Morgan"))
    context, result, note, job_id, _ = await recorded(
        store, standin, SHORT, speaker_answer(alex_id), attendees_field=""
    )
    detail = await context.lifecycle_detail({"note_id": note.id})
    assert "no attendees property is set up" in detail["speaker_attendees_notice"]
    assert "Model connections" in detail["speaker_attendees_notice"]
    completed = await context.complete_review(await completion(context, store, result, job_id))
    assert completed["state"] == "complete"
    text = (await store.get_source_artifact(result.source.id, 2, "transcript.txt")).content
    assert "Alex Morgan: Hello." in text and "Speaker 2" not in text
    reviewed = await store.get_note(note.id)
    assert reviewed.frontmatter["reviewed"] is True
    assert not reviewed.frontmatter.get("attendees")
    assert alex["path"] not in str(reviewed.frontmatter)


async def test_a_retired_person_is_reported_for_that_speaker_and_the_rest_stays_confirmable(
    store, standin
):
    from coppermind.entities import EntityChange

    alex = await store.entities.create(EntityInput(kind="person", name="Alex Morgan"))
    context, result, note, job_id, observed = await recorded(
        store, standin, SHORT, speaker_answer(alex_id)
    )
    await store.entities.change(alex["id"], EntityChange(version=alex["version"], action="retire"))
    payload = await completion(context, store, result, job_id)
    with pytest.raises(ValueError, match="Speaker 2: Alex Morgan is no longer an active person"):
        await context.complete_review(payload)
    assert (await store.get_source(result.source.id)).current_revision == 1
    leave_unnamed = await completion(context, store, result, job_id, person="")
    assert (await context.complete_review(leave_unnamed))["state"] == "complete"
    assert (await store.get_source(result.source.id)).current_revision == 1


async def test_pending_voice_signatures_are_removed_when_no_speaker_is_named(store, standin):
    from coppermind_store.voice_store import VoiceStore

    alex = await store.entities.create(EntityInput(kind="person", name="Alex Morgan"))
    context, result, note, job_id, _ = await recorded(
        store, standin, SHORT, speaker_answer(alex_id)
    )
    recording = next(r for r in context.recordings.jobs() if r.note_id == note.id)
    vector = {"model": "model", "version": "v1", "vector": [1.0, 0.0]}
    context.write(
        "voice-pending/" + recording.id,
        [{"label": "Speaker 1", "speech_seconds": 30.0, "embeddings": [vector]}],
    )
    leave_unnamed = await completion(context, store, result, job_id, person="")
    assert (await context.complete_review(leave_unnamed))["state"] == "complete"
    assert context.read("voice-pending/" + recording.id, None) is None
    assert VoiceStore(context).summary(alex["id"])["signatures"] == []


async def test_speakers_and_an_attendee_suggestion_complete_together_without_losing_either(
    store, standin
):
    alex = await store.entities.create(
        EntityInput(kind="person", name="Alex Morgan", emails=["alex@example.test"])
    )

    def answer(evidence, event):
        casey = event["people"][0]
        return [
            {
                "kind": "attendee",
                "event_ids": [event["event_id"]],
                "contact_ids": [casey["contact_id"]],
                "quote": "Casey attended",
                "confidence": 0.5,
                "reason": "Spoke in the meeting.",
            },
            *speaker_answer(alex_id)(evidence, event),
        ]

    context, result, note, job_id, observed = await recorded(store, standin, SHORT, answer)
    from coppermind.entities import EntityChange

    await store.entities.change(
        alex["id"], EntityChange(version=alex["version"], action="rename", name="Alexandra Morgan")
    )
    detail = await context.lifecycle_detail({"note_id": note.id})
    by_kind = {s["kind"]: s["index"] for s in detail["suggestions"] if s["kind"] != "speaker"}
    speaker = next(s["index"] for s in detail["suggestions"] if s["kind"] == "speaker")
    note = await store.get_note(note.id)
    completed = await context.complete_review(
        {
            "note_id": note.id,
            "if_match": note.content_hash,
            "job_id": job_id,
            "selected": [by_kind["attendee"], speaker],
            "speaker_choices": {str(speaker): "person:" + alex["id"]},
            "confirmation": "Complete review",
        }
    )
    assert completed["state"] == "complete"
    attendees = (await store.get_note(note.id)).frontmatter["attendees"]
    assert "alex@example.test" in attendees
    assert attendees[:2] == ["Casey@EXAMPLE.COM", "alex@example.test"]
    assert attendees[2].startswith("[[People/Alex")
    text = (await store.get_source_artifact(result.source.id, 2, "transcript.txt")).content
    assert "Alexandra Morgan: Hello." in text
    assert len(observed) == 1


async def test_a_three_hour_recording_with_hundreds_of_people_keeps_every_suggestion_kind(
    store, standin
):
    transcript = {
        "segments": [
            {
                "start": 20 * i,
                "speaker": f"Speaker {1 + i % 2}",
                "text": f"Turn {i}: we reviewed the rollout plan and the budget. " * 4,
            }
            for i in range(540)
        ]
        + [{"start": 20000, "speaker": "Speaker 2", "text": "Thanks, Alex."}]
    }
    await store.entities.create(EntityInput(kind="person", name="Alex Morgan"))
    for i in range(300):
        await store.entities.create(EntityInput(kind="person", name=f"Person{i} Filler"))

    def answer(evidence, event):
        return [
            {
                "kind": "filename",
                "value": "Planning",
                "event_ids": [event["event_id"]],
                "reason": "The calendar event",
            },
            *speaker_answer(alex_id)(evidence, event),
        ]

    context, result, note, job_id, observed = await recorded(store, standin, transcript, answer)
    job = context.load_job(job_id)
    assert job.state == "suggested", job.error
    assert (
        len(json.dumps(observed[0])) <= context.config().assignments["enrichment"].max_input_chars
    )
    assert len(observed[0]["speaker_review"]["candidates"]) < 50
    assert {s.kind for s in job.result.suggestions} >= {"filename", "speaker"}


async def test_speaker_evidence_too_big_is_dropped_and_other_suggestions_still_arrive(
    store, standin
):
    def answer(evidence, event):
        return [
            {
                "kind": "filename",
                "value": "Planning",
                "event_ids": [event["event_id"]],
                "reason": "The calendar event",
            }
        ]

    context, result, note, job_id, observed = await recorded(
        store, standin, SHORT, answer, max_input_chars=2400
    )
    job = context.load_job(job_id)
    assert job.state == "suggested", job.error
    assert [s.kind for s in job.result.suggestions] == ["filename"]
    assert observed[0]["speaker_review"]["omitted"] is True
    detail = await context.lifecycle_detail({"note_id": note.id})
    assert detail["speaker_notice"].startswith("Speaker names could not be suggested")


async def test_forty_speakers_keep_the_other_suggestions_and_leave_extras_to_the_manual_chooser(
    store, standin
):
    transcript = {
        "segments": [
            {"start": i, "speaker": f"Speaker {i + 1}", "text": f"Turn {i}."} for i in range(40)
        ]
    }

    def answer(evidence, event):
        return [
            {
                "kind": "filename",
                "value": "Planning",
                "event_ids": [event["event_id"]],
                "reason": "The calendar event",
            }
        ]

    context, result, note, job_id, observed = await recorded(store, standin, transcript, answer)
    job = context.load_job(job_id)
    assert job.state == "suggested", job.error
    kinds = [s.kind for s in job.result.suggestions]
    assert kinds.count("filename") == 1
    assert len(job.result.suggestions) == 30
    detail = await context.lifecycle_detail({"note_id": note.id})
    assert (
        detail["speaker_notice"]
        == "11 more speakers have no suggestion. You can name them by hand below."
    )
    assert len(detail["manual_speaker_review"]["speakers"]) == 40


@pytest.mark.parametrize("action", ["rename", "retire"])
async def test_calendar_contact_tracks_matched_dossier_changes(store, standin, action):
    from coppermind.entities import EntityChange

    person = await store.entities.create(
        EntityInput(kind="person", name="Casey", emails=["Casey@example.com"])
    )

    def calendar_candidate(evidence):
        return next(
            c["id"]
            for c in evidence["speaker_review"]["candidates"]
            if c["id"].startswith("contact:")
        )

    context, result, note, job_id, _ = await recorded(
        store, standin, SHORT, speaker_answer(calendar_candidate)
    )
    change = (
        EntityChange(version=person["version"], action="rename", name="Casey Updated")
        if action == "rename"
        else EntityChange(version=person["version"], action="retire")
    )
    await store.entities.change(person["id"], change)
    detail = await context.lifecycle_detail({"note_id": note.id})
    suggestion = next(s for s in detail["suggestions"] if s["field"] == "Speaker 2")
    contacts = [c for c in suggestion["candidates"] if c["id"].startswith("contact:")]
    if action == "rename":
        assert contacts[0]["name"] == "Casey Updated"
        completed = await context.complete_review(await completion(context, store, result, job_id))
        assert completed["state"] == "complete"
        text = (await store.get_source_artifact(result.source.id, 2, "transcript.txt")).content
        assert "Casey Updated: Hello." in text
        current = await store.entities.get(person["id"])
        reviewed = await store.get_note(note.id)
        assert (
            "[[" + current["path"].removesuffix(".md") + "]]" in reviewed.frontmatter["attendees"]
        )
    else:
        assert not contacts and suggestion["value"] is None
        assert "no longer active" in suggestion["reason"]
        from coppermind_store import speaker_names

        refreshed = await speaker_names.evidence(
            context, note, await context.calendar_candidates(note)
        )
        assert not any(c["id"].startswith("contact:") for c in refreshed["candidates"])
        with pytest.raises(ValueError, match="no longer an active person"):
            await context.complete_review(await completion(context, store, result, job_id))
        assert (await store.get_source(result.source.id)).current_revision == 1


async def test_public_review_api_combines_speaker_names_and_edited_summary(store, standin):
    from coppermind_store.auth import InternalAuth
    from coppermind_store.context_api import router as internal_router
    from fastapi import FastAPI

    from coppermind.store_client import HttpStoreClient
    from tests.integration.test_store_write_protocol import authorized_api

    alex = await store.entities.create(EntityInput(kind="person", name="Alex Morgan"))
    blair = await store.entities.create(EntityInput(kind="person", name="Blair Stone"))
    transcript = {
        "segments": [
            {"start": 0, "speaker": "Speaker 1", "text": "Alex here."},
            {"start": 3, "speaker": "Speaker 2", "text": "Blair here."},
            {"start": 6, "speaker": "Speaker 3", "text": "Hello."},
        ]
    }

    def answer(evidence, event):
        alex_candidate = next(
            c["id"] for c in evidence["speaker_review"]["candidates"] if c["name"] == "Alex Morgan"
        )
        return [
            {
                "kind": "speaker",
                "field": label,
                "value": alex_candidate,
                "confidence": 0.9,
                "reason": "Named in the conversation.",
            }
            for label in ("Speaker 1", "Speaker 2")
        ]

    summary = {
        "summary": "Alex and Blair discussed the launch.",
        "decisions": "- Launch Friday.",
        "action_items": "- Alex: Send the plan.",
    }
    context, result, note, job_id, _ = await recorded(
        store, standin, transcript, answer, summary=summary
    )
    internal = FastAPI()
    internal.state.store = store
    internal.state.auth = InternalAuth("synthetic-internal-token")
    internal.include_router(internal_router)
    client = HttpStoreClient(
        "http://store", "synthetic-internal-token", transport=httpx.ASGITransport(app=internal)
    )
    app, headers = authorized_api(store)
    app.state.store = client
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api", headers=headers
    ) as api:
        detail = await api.get(f"/v1/notes/{note.id}/review")
        assert detail.status_code == 200, detail.text
        assert detail.json()["meeting_summary"] == summary
        proposals = {s["field"]: s for s in detail.json()["suggestions"] if s["kind"] == "speaker"}
        first, second = proposals["Speaker 1"], proposals["Speaker 2"]
        response = await api.post(
            f"/v1/notes/{note.id}/review",
            json={
                "if_match": note.content_hash,
                "confirmation": "Complete review",
                "job_id": job_id,
                "selected": [first["index"], second["index"]],
                "meeting_summary": {
                    **detail.json()["meeting_summary"],
                    "decisions": "- Launch Monday.",
                    "action_items": "",
                },
                "speaker_choices": {
                    str(first["index"]): "person:" + alex["id"],
                    str(second["index"]): "person:" + blair["id"],
                },
            },
        )
        assert response.status_code == 200, response.text
    source = await store.get_source(result.source.id)
    text = (
        await store.get_source_artifact(result.source.id, source.current_revision, "transcript.txt")
    ).content
    assert "Alex Morgan: Alex here." in text
    assert "Blair Stone: Blair here." in text
    assert "Speaker 3: Hello." in text
    reviewed = await store.get_note(note.id)
    assert reviewed.frontmatter["reviewed"] is True
    assert reviewed.body.startswith(note.body)
    assert summary["summary"] in reviewed.body
    assert "- Launch Monday." in reviewed.body
    assert summary["decisions"] not in reviewed.body
    assert summary["action_items"] not in reviewed.body
    assert "## Action items" not in reviewed.body


async def test_one_person_cannot_name_two_speakers(store, standin):
    alex = await store.entities.create(EntityInput(kind="person", name="Alex Morgan"))
    context, result, note, job_id, _ = await recorded(
        store, standin, SHORT, speaker_answer(alex_id)
    )
    detail = await context.lifecycle_detail({"note_id": note.id})
    payload = {
        **await completion(context, store, result, job_id),
        "manual_source_id": detail["manual_speaker_review"]["source_id"],
        "manual_source_revision": detail["manual_speaker_review"]["revision"],
        "manual_speakers": {"Speaker 1": "person:" + alex["id"]},
    }
    with pytest.raises(ValueError, match="Alex Morgan is already chosen for Speaker"):
        await context.complete_review(payload)
    assert (await store.get_source(result.source.id)).current_revision == 1
    payload["manual_speakers"] = {}
    assert (await context.complete_review(payload))["state"] == "complete"
    text = (await store.get_source_artifact(result.source.id, 2, "transcript.txt")).content
    assert "Alex Morgan: Hello." in text and "Speaker 1: Thanks, Alex." in text


@pytest.mark.parametrize("state", ["failed", "omitted", "disabled"])
async def test_manual_speaker_works_after_model_failure_and_can_pick_any_person(
    store, standin, state
):
    await store.entities.create(EntityInput(kind="person", name="Alex Morgan"))
    other = await store.entities.create(EntityInput(kind="person", name="Unmentioned Person"))
    context, result, note, job_id, _ = await recorded(
        store, standin, SHORT, speaker_answer(alex_id)
    )
    job = context.load_job(job_id)
    if state == "failed":
        job.state = "failed"
        job.result = None
        job.set_error("The model timed out.")
    elif state == "omitted":
        job.result.suggestions = []
        job.evidence["speaker_review"]["omitted"] = True
    else:
        config = context.config()
        config.assignments["enrichment"].enabled = False
        context.save_config(config)
        job.result = None
    context.save_job(job)
    detail = await context.lifecycle_detail({"note_id": note.id})
    if state == "failed":
        assert detail["suggestion_error"] == "The model timed out."
    assert "Speaker 2" in detail["manual_speaker_review"]["speakers"]
    assert "person:" + other["id"] not in {
        c["id"] for c in detail["manual_speaker_review"]["candidates"]
    }
    payload = {
        "note_id": note.id,
        "if_match": note.content_hash,
        "confirmation": "Complete review",
        "selected": [],
        "manual_source_revision": detail["manual_speaker_review"]["revision"],
        "manual_source_id": detail["manual_speaker_review"]["source_id"],
        "manual_speakers": {"Speaker 2": "person:" + other["id"]},
    }
    with pytest.raises(ValueError, match="transcript changed"):
        await context.complete_review({**payload, "manual_source_revision": 99})
    assert (await store.get_source(result.source.id)).current_revision == 1
    receipt = await context.complete_review(payload)
    assert receipt["state"] == "complete"
    assert await context.complete_review(payload) == receipt
    assert (await store.get_source(result.source.id)).current_revision == 2
    text = (await store.get_source_artifact(result.source.id, 2, "transcript.txt")).content
    assert "Unmentioned Person: Hello." in text


@pytest.mark.parametrize("bad", [["list"], "", "unknown", "person:missing", "duplicate"])
async def test_a_bad_speaker_value_leaves_only_that_speaker_unnamed(store, standin, bad):
    await store.entities.create(EntityInput(kind="person", name="Alex Morgan"))

    def entry(label, value):
        return {
            "kind": "speaker",
            "field": label,
            "value": value,
            "confidence": 0.9,
            "reason": "Addressed by name.",
        }

    def answer(evidence, event):
        alex = alex_id(evidence)
        second = [entry("Speaker 2", alex), entry("Speaker 2", alex)]
        return [
            entry("Speaker 1", alex),
            *(second if bad == "duplicate" else [entry("Speaker 2", bad)]),
        ]

    context, result, note, job_id, observed = await recorded(store, standin, SHORT, answer)
    job = context.load_job(job_id)
    assert job.state == "suggested"
    proposals = {s.field: s for s in job.result.suggestions if s.kind == "speaker"}
    assert proposals["Speaker 1"].value == alex_id(observed[0])
    assert proposals["Speaker 2"].value is None
    assert proposals["Speaker 2"].reason == "No confident match was suggested."


async def test_a_speaker_quote_not_in_the_transcript_is_not_shown_as_evidence(store, standin):
    await store.entities.create(EntityInput(kind="person", name="Alex Morgan"))

    def entry(label, value, quote):
        return {
            "kind": "speaker",
            "field": label,
            "value": value,
            "confidence": 0.9,
            "reason": "Addressed by name.",
            "quote": quote,
        }

    def answer(evidence, event):
        alex = alex_id(evidence)
        return [
            entry("Speaker 1", alex, "Thanks, Alex."),
            entry("Speaker 2", alex, "Thanks Alex, over to you"),
        ]

    context, result, note, job_id, observed = await recorded(store, standin, SHORT, answer)
    job = context.load_job(job_id)
    proposals = {s.field: s for s in job.result.suggestions if s.kind == "speaker"}
    assert proposals["Speaker 1"].quote == "Thanks, Alex."
    assert proposals["Speaker 2"].quote == ""
    assert proposals["Speaker 2"].value == alex_id(observed[0])


async def test_voice_enrollment_keeps_every_named_speaker_and_removes_pending(store, standin):
    from coppermind_store.voice_store import VoiceStore

    alex = await store.entities.create(EntityInput(kind="person", name="Alex Morgan"))
    blair = await store.entities.create(EntityInput(kind="person", name="Blair Stone"))
    context, result, note, _, _ = await recorded(store, standin, SHORT, speaker_answer(alex_id))
    voice = VoiceStore(context)
    recording = next(r for r in context.recordings.jobs() if r.note_id == note.id)
    vector = {"model": "model", "version": "v1", "vector": [1.0, 0.0]}
    context.write(
        "voice-pending/" + recording.id,
        [
            {"label": "Speaker 1", "speech_seconds": 30.0, "embeddings": [vector]},
            {"label": "Speaker 2", "speech_seconds": 30.0, "embeddings": [vector]},
        ],
    )
    detail = await context.lifecycle_detail({"note_id": note.id})
    manual = detail["manual_speaker_review"]
    assert "vector" not in json.dumps(detail)
    await context.complete_review(
        {
            "note_id": note.id,
            "if_match": note.content_hash,
            "confirmation": "Complete review",
            "selected": [],
            "manual_source_id": manual["source_id"],
            "manual_source_revision": manual["revision"],
            "manual_speakers": {
                "Speaker 1": "person:" + alex["id"],
                "Speaker 2": "person:" + blair["id"],
            },
        }
    )
    assert len(voice.summary(alex["id"])["signatures"]) == 1
    assert len(voice.summary(blair["id"])["signatures"]) == 1
    assert context.read("voice-pending/" + recording.id, None) is None
    manifest = await store.get_source(result.source.id)
    artifact = await store.get_source_artifact(
        result.source.id, manifest.current_revision, "transcript.json"
    )
    assert "vector" not in artifact.content
    context.write("voice-pending/" + recording.id, [{"label": "Speaker 1", "embeddings": [vector]}])
    store.entities.purge({note.id}, {alex["id"]}, "voice-purge-test")
    assert context.read("voice-pending/" + recording.id, None) is None
    assert voice.summary(alex["id"])["signatures"] == []


async def test_naming_an_unknown_voice_with_a_calendar_contact_gives_it_a_person(store, standin):
    from coppermind_store.voice_store import VoiceStore

    transcript = {
        "segments": [
            {"start": 0, "speaker": "Unknown voice 1", "text": "Hello."},
            {"start": 3, "speaker": "Unknown voice 2", "text": "Hi."},
        ]
    }
    context, result, note, _, _ = await recorded(store, standin, transcript, lambda e, ev: [])
    vector = {"model": "model", "version": "v1", "vector": [1.0, 0.0]}
    other = {"model": "model", "version": "v1", "vector": [0.0, 1.0]}
    voice = VoiceStore(context)
    assert voice.capture_unknown(note.id, "Unknown voice 1", "today", 30, [vector]) == "unknown:1"
    assert voice.capture_unknown(note.id, "Unknown voice 2", "today", 30, [other]) == "unknown:2"
    voice.set_segments(note.id, transcript)
    manual = (await context.lifecycle_detail({"note_id": note.id}))["manual_speaker_review"]
    contact = next(c for c in manual["candidates"] if c["id"].startswith("contact:"))
    assert not any(p.name == contact["name"] for p in store.entities.load().entities.values())
    completed = await context.complete_review(
        {
            "note_id": note.id,
            "if_match": note.content_hash,
            "confirmation": "Complete review",
            "selected": [],
            "manual_source_id": manual["source_id"],
            "manual_source_revision": manual["revision"],
            "manual_speakers": {"Unknown voice 1": contact["id"]},
        }
    )
    assert completed["state"] == "complete"
    people = [
        p
        for p in store.entities.load().entities.values()
        if p.kind == "person" and p.name == contact["name"]
    ]
    assert len(people) == 1
    assert list(voice.unknown_summaries()) == ["unknown:2"]
    assert len(voice.summary(people[0].id)["signatures"]) == 1
    assert [a["note_id"] for a in voice.assignments("person:" + people[0].id)] == [note.id]
    manifest = await store.get_source(result.source.id)
    text = (
        await store.get_source_artifact(
            result.source.id, manifest.current_revision, "transcript.txt"
        )
    ).content
    assert contact["name"] + ": Hello." in text
    assert "Unknown voice 1" not in text


async def test_a_contact_is_not_attached_to_a_same_named_person_with_another_address(
    store, standin
):
    from coppermind_store.voice_store import VoiceStore

    transcript = {"segments": [{"start": 0, "speaker": "Unknown voice 1", "text": "Hello."}]}
    context, result, note, _, _ = await recorded(store, standin, transcript, lambda e, ev: [])
    manual = (await context.lifecycle_detail({"note_id": note.id}))["manual_speaker_review"]
    contact = next(c for c in manual["candidates"] if c["id"].startswith("contact:"))
    assert contact["email"]
    namesake = await store.entities.create(
        EntityInput(kind="person", name=contact["name"], emails=["someone.else@example.test"])
    )
    voice = VoiceStore(context)
    vector = {"model": "model", "version": "v1", "vector": [1.0, 0.0]}
    voice.capture_unknown(note.id, "Unknown voice 1", "today", 30, [vector])
    voice.set_segments(note.id, transcript)
    completed = await context.complete_review(
        {
            "note_id": note.id,
            "if_match": note.content_hash,
            "confirmation": "Complete review",
            "selected": [],
            "manual_source_id": manual["source_id"],
            "manual_source_revision": manual["revision"],
            "manual_speakers": {"Unknown voice 1": contact["id"]},
        }
    )
    assert completed["state"] == "complete"
    assert voice.summary(namesake["id"])["signatures"] == []
    invitee = next(
        p
        for p in store.entities.load().entities.values()
        if p.kind == "person" and p.name == contact["name"] and p.id != namesake["id"]
    )
    assert invitee.emails and "someone.else@example.test" not in invitee.emails
    assert len(voice.summary(invitee.id)["signatures"]) == 1
    assert voice.unknown_summaries() == {}


async def contact_review(store, standin):
    transcript = {
        "segments": [
            {"start": 0, "speaker": "Unknown voice 1", "text": "Hello."},
            {"start": 3, "speaker": "Unknown voice 2", "text": "Hi."},
        ]
    }
    context, result, note, _, _ = await recorded(store, standin, transcript, lambda e, ev: [])
    manual = (await context.lifecycle_detail({"note_id": note.id}))["manual_speaker_review"]
    contact = next(c for c in manual["candidates"] if c["id"].startswith("contact:"))

    def payload(speakers):
        return {
            "note_id": note.id,
            "if_match": note.content_hash,
            "confirmation": "Complete review",
            "selected": [],
            "manual_source_id": manual["source_id"],
            "manual_source_revision": manual["revision"],
            "manual_speakers": speakers,
        }

    def people():
        return [p for p in store.entities.load().entities.values() if p.kind == "person"]

    return context, result, contact, payload, people


async def test_a_refused_review_creates_no_person_for_a_calendar_contact(store, standin):
    context, result, contact, payload, people = await contact_review(store, standin)
    before = len(people())
    with pytest.raises(ValueError, match="is already chosen for"):
        await context.complete_review(
            payload({"Unknown voice 1": contact["id"], "Unknown voice 2": contact["id"]})
        )
    assert len(people()) == before
    assert (await store.get_source(result.source.id)).current_revision == 1


async def test_a_review_that_fails_after_a_contact_is_accepted_creates_no_person(store, standin):
    context, result, contact, payload, people = await contact_review(store, standin)
    before = len(people())
    with pytest.raises(ValueError, match="no longer active"):
        await context.complete_review(
            payload({"Unknown voice 1": contact["id"], "Unknown voice 2": "person:missing"})
        )
    assert len(people()) == before
    assert (await store.get_source(result.source.id)).current_revision == 1


async def test_retrying_after_an_interrupted_contact_review_reuses_the_created_person(
    store, standin, monkeypatch
):
    import copy

    from coppermind_store import speaker_names

    context, result, contact, payload, people = await contact_review(store, standin)
    request = payload({"Unknown voice 1": contact["id"]})
    create = speaker_names.create_contact_people

    async def interrupted(ctx, operation):
        await create(ctx, copy.deepcopy(operation))
        raise RuntimeError("interrupted before the receipt was saved")

    monkeypatch.setattr(speaker_names, "create_contact_people", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        await context.complete_review(request)
    assert [p.name for p in people()].count(contact["name"]) == 1
    monkeypatch.setattr(speaker_names, "create_contact_people", create)
    completed = await context.complete_review(request)
    assert completed["state"] == "complete"
    assert [p.name for p in people()].count(contact["name"]) == 1
    manifest = await store.get_source(result.source.id)
    text = (
        await store.get_source_artifact(
            result.source.id, manifest.current_revision, "transcript.txt"
        )
    ).content
    assert contact["name"] + ": Hello." in text


async def test_a_contact_whose_address_belongs_to_a_removed_person_stops_the_review(store, standin):
    from coppermind.entities import EntityChange

    def calendar_candidate(evidence):
        return next(
            c["id"]
            for c in evidence["speaker_review"]["candidates"]
            if c["id"].startswith("contact:")
        )

    context, result, note, job_id, observed = await recorded(
        store, standin, SHORT, speaker_answer(calendar_candidate)
    )
    address = next(
        c["email"]
        for c in observed[0]["speaker_review"]["candidates"]
        if c["id"].startswith("contact:")
    )
    removed = await store.entities.create(
        EntityInput(kind="person", name="Someone Else", emails=[address])
    )
    await store.entities.change(
        removed["id"], EntityChange(version=removed["version"], action="retire")
    )
    before = len(store.entities.load().entities)
    request = await completion(context, store, result, job_id)
    with pytest.raises(ValueError, match="matches a person who was removed"):
        await context.complete_review(request)
    assert len(store.entities.load().entities) == before
    assert (await store.get_source(result.source.id)).current_revision == 1


async def test_a_contact_whose_address_belongs_to_a_company_stops_the_review_before_any_receipt(
    store, standin
):
    context, result, contact, payload, people = await contact_review(store, standin)
    await store.entities.create(
        EntityInput(kind="company", name="Vendor Support", emails=[contact["email"]])
    )
    before = len(store.entities.load().entities)
    with pytest.raises(ValueError, match="already belongs to Vendor Support"):
        await context.complete_review(payload({"Unknown voice 1": contact["id"]}))
    assert len(store.entities.load().entities) == before
    assert context.read("review-completions/" + result.note.id, {}) == {}
    assert (await store.get_source(result.source.id)).current_revision == 1


async def test_an_address_taken_after_validation_can_be_fixed_and_the_review_resumed(
    store, standin, monkeypatch
):
    from coppermind_store import speaker_names

    from coppermind.entities import EntityChange

    context, result, contact, payload, people = await contact_review(store, standin)
    request = payload({"Unknown voice 1": contact["id"]})
    create = speaker_names.create_contact_people
    holder: dict[str, Any] = {}

    async def taken_meanwhile(ctx, operation):
        holder.update(
            await store.entities.create(
                EntityInput(kind="company", name="Vendor Support", emails=[contact["email"]])
            )
        )
        await create(ctx, operation)

    monkeypatch.setattr(speaker_names, "create_contact_people", taken_meanwhile)
    with pytest.raises(ValueError, match="now belongs to Vendor Support.*resume the review"):
        await context.complete_review(request)
    assert contact["name"] not in [p.name for p in people()]
    monkeypatch.setattr(speaker_names, "create_contact_people", create)
    await store.entities.change(
        holder["id"],
        EntityChange(version=holder["version"], action="aliases", emails=[]),
    )
    completed = await context.complete_review(request)
    assert completed["state"] == "complete"
    assert [p.name for p in people()].count(contact["name"]) == 1


async def deleted_note_voice(store, standin):
    import sqlalchemy as sa
    from coppermind_store.voice_store import VoiceStore

    from coppermind.db.models import Note
    from coppermind.store_protocol import CreateNote

    transcript = {"segments": [{"start": 0, "speaker": "Unknown voice 1", "text": "Hello."}]}
    context, result, note, _, _ = await recorded(store, standin, transcript, lambda e, ev: [])
    vector = {"model": "model", "version": "v1", "vector": [1.0, 0.0]}
    voice = VoiceStore(context)
    earlier = await store.create_note(CreateNote(title="Earlier", body="Earlier recording"))
    assert voice.capture_unknown(earlier.id, "Speaker 1", "today", 30, [vector]) == "unknown:1"
    assert voice.capture_unknown(note.id, "Unknown voice 1", "today", 30, [vector]) == "unknown:1"
    voice.set_segments(note.id, transcript)
    async with store.session_factory() as session:
        await session.execute(sa.update(Note).where(Note.id == earlier.id).values(state="deleted"))
        await session.commit()
    return context, result, note, earlier, voice


async def test_naming_a_voice_still_holding_a_deleted_notes_signature_completes(store, standin):
    context, result, note, earlier, voice = await deleted_note_voice(store, standin)
    manual = (await context.lifecycle_detail({"note_id": note.id}))["manual_speaker_review"]
    contact = next(c for c in manual["candidates"] if c["id"].startswith("contact:"))
    completed = await context.complete_review(
        {
            "note_id": note.id,
            "if_match": note.content_hash,
            "confirmation": "Complete review",
            "selected": [],
            "manual_source_id": manual["source_id"],
            "manual_source_revision": manual["revision"],
            "manual_speakers": {"Unknown voice 1": contact["id"]},
        }
    )
    assert completed["state"] == "complete"
    person = next(
        p
        for p in store.entities.load().entities.values()
        if p.kind == "person" and p.name == contact["name"]
    )
    assert [s["note_id"] for s in voice.summary(person.id)["signatures"]] == [note.id]
    assert [a["note_id"] for a in voice.assignments("person:" + person.id)] == [note.id]
    assert voice.unknown_summaries() == {}


async def test_removing_a_deleted_notes_recording_drops_its_voice_data(store, standin):
    context, result, note, earlier, voice = await deleted_note_voice(store, standin)
    context.recordings.save(
        TranscriptionJob(
            id="c" * 64,
            artifact_id="d" * 64,
            filename="earlier.wav",
            mime_type="audio/wav",
            size_bytes=1,
            title="Earlier",
            provider="test-audio",
            external_source_id="earlier",
            state="succeeded",
            transcribed=True,
            created_at="2026-10-13T14:00:00+00:00",
            note_id=earlier.id,
        )
    )
    job = next(j for j in context.recordings.jobs() if j.note_id == earlier.id)
    await context.recordings.remove_reviewed_audio(job)
    kept = voice.unknown_summaries()["unknown:1"]
    assert [s["note_id"] for s in kept["signatures"]] == [note.id]
    assert [a["note_id"] for a in kept["assignments"]] == [note.id]


async def test_review_requires_people_write_only_when_creating_a_contact_person(store, standin):
    import httpx
    from coppermind_api.auth import ApiKeyAuthenticator
    from coppermind_store.auth import InternalAuth
    from coppermind_store.context_api import router as context_router
    from fastapi import FastAPI

    from coppermind.api_keys import ApiKeySet, create_key
    from coppermind.store_client import HttpStoreClient
    from tests.integration.test_store_write_protocol import authorized_api

    context, result, contact, payload, people = await contact_review(store, standin)
    existing = await store.entities.create(EntityInput(kind="person", name="Existing Person"))
    record, credential = create_key(
        "restricted",
        ["notes:read", "notes:write", "sources:read", "sources:write", "entities:read"],
        key_id="1234567890abcdef",
        secret="test-only-integration-secret",
    )
    internal = FastAPI()
    internal.state.store = store
    internal.state.auth = InternalAuth("synthetic-internal-token")
    internal.include_router(context_router)
    client = HttpStoreClient(
        "http://store", "synthetic-internal-token", transport=httpx.ASGITransport(app=internal)
    )
    app, _ = authorized_api(store)
    store.control.store.write(
        "keys",
        ApiKeySet(keys=[record]).model_dump(mode="json"),
        if_revision=store.control.store.read("keys").revision,
    )
    app.state.api_key_auth = ApiKeyAuthenticator(store)
    app.state.store = client
    before = len(people())
    choices = payload({"Unknown voice 1": contact["id"]})
    note_id = choices.pop("note_id")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api",
        headers={"Authorization": f"Bearer {credential}"},
    ) as api:
        refused = await api.post(f"/v1/notes/{note_id}/review", json=choices)
        assert refused.status_code == 403, refused.text
        assert "cannot create a person" in refused.text
        assert len(people()) == before
        assert context.read("review-completions/" + note_id, None) is None
        assert (await store.get_source(result.source.id)).current_revision == 1
        choices["manual_speakers"] = {"Unknown voice 1": "person:" + existing["id"]}
        accepted = await api.post(f"/v1/notes/{note_id}/review", json=choices)
        assert accepted.status_code == 200, accepted.text


async def test_naming_a_voice_is_not_blocked_by_another_notes_unfinished_review(store, standin):
    from coppermind_store.voice_relabel import apply_deferred
    from coppermind_store.voice_store import VoiceStore

    from coppermind.store_protocol import CreateNote

    transcript = {"segments": [{"start": 0, "speaker": "Unknown voice 1", "text": "Hello."}]}
    context, result, note, _, _ = await recorded(store, standin, transcript, lambda e, ev: [])
    vector = {"model": "model", "version": "v1", "vector": [1.0, 0.0]}
    voice = VoiceStore(context)
    other = await store.create_note(CreateNote(title="Other", body="Other recording"))
    assert voice.capture_unknown(other.id, "Speaker 1", "today", 30, [vector]) == "unknown:1"
    assert voice.capture_unknown(note.id, "Unknown voice 1", "today", 30, [vector]) == "unknown:1"
    voice.set_segments(note.id, transcript)
    context.write("review-completions/" + other.id, {"state": "applying"})
    manual = (await context.lifecycle_detail({"note_id": note.id}))["manual_speaker_review"]
    contact = next(c for c in manual["candidates"] if c["id"].startswith("contact:"))

    completed = await context.complete_review(
        {
            "note_id": note.id,
            "if_match": note.content_hash,
            "confirmation": "Complete review",
            "selected": [],
            "manual_source_id": manual["source_id"],
            "manual_source_revision": manual["revision"],
            "manual_speakers": {"Unknown voice 1": contact["id"]},
        }
    )

    assert completed["state"] == "complete"
    person = next(
        p
        for p in store.entities.load().entities.values()
        if p.kind == "person" and p.name == contact["name"]
    )
    assert voice.unknown_summaries() == {}
    assert sorted(a["note_id"] for a in voice.assignments("person:" + person.id)) == sorted(
        [note.id, other.id]
    )
    assert [(d["note_id"], d["name"]) for d in voice.deferred_relabels()] == [
        (other.id, contact["name"])
    ]
    assert not await apply_deferred(context)
    context.write("review-completions/" + other.id, {"state": "complete"})
    assert await apply_deferred(context)
    assert voice.deferred_relabels() == []


async def test_completing_a_review_applies_the_relabels_held_back_for_that_note(store, standin):
    from coppermind_store.voice_store import VoiceStore

    context, result, contact, payload, people = await contact_review(store, standin)
    note_id = payload({})["note_id"]
    voice = VoiceStore(context)
    voice.defer_relabel(
        {"note_id": note_id, "label": "Unknown voice 2", "segments": [1]}, contact["name"]
    )

    await context.complete_review(payload({"Unknown voice 1": contact["id"]}))

    assert voice.deferred_relabels() == []
    manifest = await store.get_source(result.source.id)
    text = (
        await store.get_source_artifact(
            result.source.id, manifest.current_revision, "transcript.txt"
        )
    ).content
    assert contact["name"] + ": Hi." in text


async def reviewed_with_unknown_voice(store, standin):
    """A reviewed, filed note whose second voice stayed unknown, with that voice captured."""
    from coppermind_store.voice_store import VoiceStore

    context, result, contact, payload, people = await contact_review(store, standin)
    note_id = payload({})["note_id"]
    voice = VoiceStore(context)
    vector = {"model": "model", "version": "v1", "vector": [1.0, 0.0]}
    other = {"model": "model", "version": "v1", "vector": [0.0, 1.0]}
    voice.capture_unknown(note_id, "Unknown voice 1", "today", 30, [vector])
    voice.capture_unknown(note_id, "Unknown voice 2", "today", 30, [other])
    voice.set_segments(
        note_id, {"segments": [{"speaker": "Unknown voice 1"}, {"speaker": "Unknown voice 2"}]}
    )
    completed = await context.complete_review(payload({"Unknown voice 1": contact["id"]}))
    assert completed["state"] == "complete"
    # Leaving a voice unknown, and no summary accepted, did not stop the review.
    assert voice.unknown_summaries()["unknown:2"]["name"] == "Unknown voice 2"
    return context, store, note_id, voice


async def test_a_voice_identified_after_review_adds_that_person_to_attendees_once(store, standin):
    from coppermind_store.voice_relabel import combine

    context, _, note_id, voice = await reviewed_with_unknown_voice(store, standin)
    blair = await store.entities.create(
        EntityInput(kind="person", name="Blair Stone", emails=["blair@example.test"])
    )
    before = (await store.get_note(note_id)).frontmatter["attendees"]
    async with context.lifecycle.lock, context.lock:
        await combine(context, "unknown:2", "person:" + blair["id"])
    after = (await store.get_note(note_id)).frontmatter["attendees"]
    assert after[: len(before)] == before
    assert after.count("blair@example.test") == 1
    logged = context.read("review-completions/" + note_id, {})["late_attendees"]
    assert [(e["name"], e["label"]) for e in logged] == [("Blair Stone", "Unknown voice 2")]
    # The same person identified again changes nothing.
    voice.restore_unknown("unknown:2")
    async with context.lifecycle.lock, context.lock:
        await combine(context, "unknown:2", "person:" + blair["id"])
    assert (await store.get_note(note_id)).frontmatter["attendees"] == after
    assert len(context.read("review-completions/" + note_id, {})["late_attendees"]) == 1


async def test_someone_already_listed_is_not_added_again(store, standin):
    from coppermind_store.voice_relabel import combine

    from coppermind.store_protocol import PatchFrontmatter

    context, _, note_id, _ = await reviewed_with_unknown_voice(store, standin)
    blair = await store.entities.create(
        EntityInput(kind="person", name="Blair Stone", emails=["blair@example.test"])
    )
    note = await store.get_note(note_id)
    listed = [*note.frontmatter["attendees"], "Blair@Example.test"]
    await store.patch_frontmatter(
        note_id, PatchFrontmatter(set={"attendees": listed}), note.content_hash
    )
    async with context.lifecycle.lock, context.lock:
        await combine(context, "unknown:2", "person:" + blair["id"])
    assert (await store.get_note(note_id)).frontmatter["attendees"] == listed
    assert "late_attendees" not in context.read("review-completions/" + note_id, {})


@pytest.mark.parametrize("limit", ["never_this_person", "recognition_off"])
async def test_a_refused_or_switched_off_voice_adds_no_attendee(store, standin, limit):
    from coppermind_store.voice_relabel import combine

    context, _, note_id, voice = await reviewed_with_unknown_voice(store, standin)
    blair = await store.entities.create(EntityInput(kind="person", name="Blair Stone"))
    if limit == "never_this_person":
        voice.reject(note_id, "Unknown voice 2", blair["id"])
    else:
        voice.set_recognition_enabled(False)
    before = (await store.get_note(note_id)).frontmatter["attendees"]
    async with context.lifecycle.lock, context.lock:
        await combine(context, "unknown:2", "person:" + blair["id"])
    assert (await store.get_note(note_id)).frontmatter["attendees"] == before


async def test_a_note_still_in_review_gets_attendees_only_from_its_review(store, standin):
    from coppermind_store.voice_relabel import combine
    from coppermind_store.voice_store import VoiceStore

    context, result, contact, payload, people = await contact_review(store, standin)
    note_id = payload({})["note_id"]
    voice = VoiceStore(context)
    vector = {"model": "model", "version": "v1", "vector": [1.0, 0.0]}
    voice.capture_unknown(note_id, "Unknown voice 1", "today", 30, [vector])
    voice.set_segments(note_id, {"segments": [{"speaker": "Unknown voice 1"}, {"speaker": "x"}]})
    blair = await store.entities.create(EntityInput(kind="person", name="Blair Stone"))
    before = (await store.get_note(note_id)).frontmatter
    async with context.lifecycle.lock, context.lock:
        await combine(context, "unknown:1", "person:" + blair["id"])
    assert (await store.get_note(note_id)).frontmatter.get("attendees") == before.get("attendees")


async def test_marking_a_note_reviewed_does_not_wait_for_unknown_speakers_or_a_summary(
    store, standin
):
    context, _, note_id, voice = await reviewed_with_unknown_voice(store, standin)
    note = await store.get_note(note_id)
    assert note.frontmatter["reviewed"] is True
    assert voice.unknown_summaries()
