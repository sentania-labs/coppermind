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
    if choice in {"failed", "second_failed"}:
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
    assert observed[0]["speaker_review"]["transcript"] == render_transcript(transcript)
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
