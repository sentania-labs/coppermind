"""Whole transcript to owner-reviewed prose through a real store and loopback model."""

import json
from typing import Any

import httpx
import pytest
from coppermind_store.model_provider import ModelProvider
from coppermind_store.recordings import TranscriptionJob, render_transcript

from coppermind.store_protocol import (
    IngestArtifact,
    IngestNote,
    IngestRequest,
    IngestSource,
    ReplaceNote,
)
from tests.integration.test_context_enrichment import configure
from tests.integration.test_speaker_names import standin as model_standin

standin = model_standin

PROPOSAL = {
    "summary": "The team discussed the launch and the final delivery date.",
    "decisions": "- Launch on Friday.",
    "action_items": "- Speaker 1: Send the release plan.",
}


async def setup_recording(store, standin, *, limit=200000, failure=None):
    context = await configure(store)
    await context.setup_lifecycle({"revision": context.config().revision})
    transcript = {
        "segments": [
            {"start": 0, "speaker": "Speaker 1", "text": "Opening. " + "Discussion. " * 14000},
            {
                "start": 3599,
                "speaker": "Speaker 1",
                "text": "Launch Friday. I will send the release plan.",
            },
        ]
    }
    text = render_transcript(transcript)
    result = await store.ingest(
        IngestRequest(
            source=IngestSource(
                provider="test-audio",
                external_source_id="summary-test",
                source_type="transcript",
                artifacts=[
                    IngestArtifact(name="transcript.txt", mime_type="text/plain", content=text),
                    IngestArtifact(
                        name="transcript.json",
                        mime_type="application/json",
                        content=json.dumps(transcript),
                    ),
                ],
            ),
            note=IngestNote(
                title="Planning",
                body="My own meeting notes.\n",
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
            external_source_id="summary-test",
            state="succeeded",
            transcribed=True,
            created_at="2026-10-14T14:00:00+00:00",
            note_id=result.note.id,
            note_path=result.note.path,
            source_id=result.source.id,
        )
    )
    requests = []

    def model(request):
        payload = json.loads(request.content)
        evidence = json.loads(payload["messages"][-1]["content"])
        requests.append(evidence)
        assert payload["model"] == "test-model"
        output: dict[str, Any]
        if "transcript" in evidence:
            assert evidence["transcript"] == text  # entire 168k input, including the ending
            if failure == "context":
                return httpx.Response(400, json={"error": {"message": "context length exceeded"}})
            output = {"summary": 42} if failure == "shape" else PROPOSAL
        else:
            output = {"suggestions": [], "unresolved": []}
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output)}}]
            },
        )

    standin["handler"] = model
    config = context.config()
    config.assignments["enrichment"].summary_max_input_chars = limit
    connection = next(
        c for c in config.connections if c.id == config.assignments["enrichment"].connection
    )
    connection.base_url = standin["url"]
    context.save_config(config)
    context.provider = ModelProvider()
    note = await store.get_note(result.note.id)
    job = context.load_job(await context.queue_note(note.id))
    await context.run_job(job)
    return context, note, job, requests


@pytest.mark.parametrize("mode", ["keep", "edit", "reject", "interrupted", "stale"])
async def test_full_transcript_summary_waits_for_owner_and_replays_once(
    store, standin, monkeypatch, mode
):
    context, note, job, requests = await setup_recording(store, standin)
    assert job.state == "suggested", job.error
    assert sum("transcript" in request for request in requests) == 1
    assert (await store.get_note(note.id)).body == note.body
    assert context.load_job(job.id).result.meeting_summary.model_dump() == PROPOSAL
    detail = await context.lifecycle_detail({"note_id": note.id})
    assert detail["meeting_summary"] == PROPOSAL
    kept = dict(PROPOSAL)
    if mode == "edit":
        kept.update(
            summary="Owner corrected summary.", decisions="", action_items="- Casey: send plan."
        )
    payload = {
        "note_id": note.id,
        "if_match": note.content_hash,
        "job_id": job.id,
        "selected": [],
        "meeting_summary": None if mode == "reject" else kept,
        "confirmation": "Complete review",
    }
    if mode == "stale":
        await store.replace_note(
            note.id,
            ReplaceNote(frontmatter=note.frontmatter, body="Later owner text."),
            note.content_hash,
        )
        with pytest.raises(ValueError, match="note changed"):
            await context.complete_review(payload)
        assert (await store.get_note(note.id)).body == "Later owner text.\n"
        return
    if mode == "interrupted":
        replace = store.replace_note

        async def interrupted(*args, **kwargs):
            await replace(*args, **kwargs)
            raise RuntimeError("crash after filesystem write")

        monkeypatch.setattr(store, "replace_note", interrupted)
        with pytest.raises(RuntimeError, match="crash"):
            await context.complete_review(payload)
        monkeypatch.setattr(store, "replace_note", replace)
    receipt = await context.complete_review(payload)
    assert receipt["state"] == "complete"
    if mode == "reject":
        # The persisted pre-summary receipt contract must remain replayable.
        from coppermind_store.context_service import fingerprint

        legacy = {
            "note_id": note.id,
            "if_match": note.content_hash,
            "job_id": job.id,
            "selected": [],
            "speaker_choices": None,
            "manual_speakers": None,
            "manual_source_revision": None,
            "manual_source_id": None,
            "accept_link_risk": None,
        }
        assert receipt["request"] == fingerprint(legacy)
    reviewed = await store.get_note(note.id)
    assert reviewed.frontmatter["reviewed"] is True
    assert reviewed.body.startswith(note.body)
    if mode == "reject":
        assert reviewed.body == note.body
    else:
        assert reviewed.body.count("## Summary") == 1
        assert kept["summary"] in reviewed.body
        assert kept["action_items"] in reviewed.body
        assert ("## Decisions" in reviewed.body) == (mode != "edit")
    assert await context.complete_review(payload) == receipt
    assert (await store.get_note(note.id)).content_hash == reviewed.content_hash


@pytest.mark.parametrize(
    "failure,rule",
    [
        ("limit", "summary-input-limit"),
        ("context", "summary-provider-error"),
        ("shape", "answer-shape"),
    ],
)
async def test_summary_failures_are_durable_problems_with_no_partial_note(
    store, standin, failure, rule
):
    context, note, job, requests = await setup_recording(
        store,
        standin,
        limit=60000 if failure == "limit" else 200000,
        failure=failure,
    )
    assert job.state == "failed", job.error
    assert rule in job.error_rule
    assert context.load_job(job.id).error == job.error
    assert (await store.get_note(note.id)).body == note.body
    assert any(
        item["id"] == job.id for item in (await context.attention({}))["failed_jobs"]["items"]
    )
    if failure == "limit":
        assert not any("transcript" in request for request in requests)
        assert "raise Maximum summary input characters" in job.error
    else:
        assert sum("transcript" in request for request in requests) == 1


async def test_raising_summary_limit_retries_the_whole_transcript(store, standin):
    context, note, job, requests = await setup_recording(store, standin, limit=60000)
    assert job.state == "failed"
    config = context.config()
    config.assignments["enrichment"].summary_max_input_chars = 300000
    context.save_config(config)
    fresh = context.load_job(await context.queue_note(note.id, force=True))
    await context.run_job(fresh)
    assert fresh.state == "suggested", fresh.error
    assert sum("transcript" in request for request in requests) == 1


async def test_summary_silence_uses_long_wait_and_records_retry_backoff(store, standin):
    context, note, _, _ = await setup_recording(store, standin)
    config = context.config()
    config.assignments["enrichment"].summary_timeout_s = 1234
    context.save_config(config)

    def timeout(request):
        evidence = json.loads(json.loads(request.content)["messages"][-1]["content"])
        if "transcript" in evidence:
            assert request.extensions["timeout"]["read"] == 1234
            raise httpx.ReadTimeout("synthetic silence", request=request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps({"suggestions": [], "unresolved": []})},
                    }
                ]
            },
        )

    context.provider = ModelProvider(httpx.MockTransport(timeout))
    job = context.load_job(await context.queue_note(note.id, force=True))
    await context.run_job(job)
    assert job.state == "failed"
    assert job.attempts == 1
    assert job.retry_after
    assert "1234" in job.error
    assert "Meeting summary failed" in job.error
    assert context.load_job(job.id).error == job.error
    assert (await store.get_note(note.id)).body == note.body
