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
    VoiceStore(context).set_enabled(alex["id"], True)
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


async def test_voice_enrollment_uses_confirmed_opted_in_people_and_removes_pending(store, standin):
    from coppermind_store.voice_store import VoiceStore

    alex = await store.entities.create(EntityInput(kind="person", name="Alex Morgan"))
    blair = await store.entities.create(EntityInput(kind="person", name="Blair Stone"))
    context, result, note, _, _ = await recorded(store, standin, SHORT, speaker_answer(alex_id))
    voice = VoiceStore(context)
    voice.set_enabled(alex["id"], True)
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
    assert voice.summary(blair["id"])["signatures"] == []
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
