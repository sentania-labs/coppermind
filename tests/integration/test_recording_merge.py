"""A confirmed recording merge survives interruptions and concurrent note edits."""

from __future__ import annotations

import pytest
from coppermind_store.enrichment_models import Suggestions
from coppermind_store.reconciler import reconcile_once
from coppermind_store.recordings import TranscriptionJob

from coppermind.meeting_summary import MeetingSummary
from coppermind.store_protocol import (
    CreateNote,
    IngestArtifact,
    IngestNote,
    IngestRequest,
    IngestSource,
    ReplaceNote,
)
from tests.integration.test_context_enrichment import configure
from tests.integration.test_recordings import store_audio


async def prepare(store):
    context = await configure(store)
    await context.setup_lifecycle({"revision": context.config().revision})
    config = context.config()
    config.assignments["enrichment"].event_field = "calendar_event"
    context.save_config(config)
    events = await context.calendar_candidates(
        type("Note", (), {"frontmatter": {"date": "2026-10-14T09:00:00"}})()
    )
    event = events[0]["event_id"]
    target = await store.create_note(
        CreateNote(
            title="My meeting",
            body="My own words.\n",
            frontmatter={"date": "2026-10-14T09:00:00", "calendar_event": event},
        )
    )
    recording = TranscriptionJob(
        id="a" * 64,
        artifact_id=await store_audio(store),
        filename="meeting.m4a",
        mime_type="audio/mp4",
        size_bytes=1,
        title="Planning",
        provider="test-audio",
        external_source_id="merge-test",
        state="succeeded",
        transcribed=True,
        created_at="2026-10-14T14:00:00+00:00",
        captured_at="2026-10-14T09:00:00-05:00",
        frontmatter={"date": "2026-10-14T09:00:00", "calendar_event": event},
    )
    transcript = {"segments": [{"start": 0, "speaker": "Speaker 1", "text": "Hello Casey."}]}
    await context.recordings.file_note(recording, transcript)
    generated = await store.get_note(recording.note_id)
    job = context.load_job(await context.queue_note(generated.id))
    summary = MeetingSummary(summary="We agreed to ship.", decisions="", action_items="")
    job.result = Suggestions(meeting_summary=summary)
    job.state = "suggested"
    context.save_job(job)
    payload = {
        "note_id": generated.id,
        "if_match": generated.content_hash,
        "target_id": target.id,
        "target_hash": target.content_hash,
        "job_id": job.id,
        "meeting_summary": summary.model_dump(),
        "confirmation": "Merge recording",
    }
    return context, recording, generated, target, payload


@pytest.mark.parametrize(
    "crash",
    [
        "attach_write",
        "manifest_write",
        "attached",
        "summary_write",
        "appended",
        "retire_prepared",
        "retired",
    ],
)
async def test_merge_replays_each_interruption_and_preserves_intervening_edit(
    store, monkeypatch, crash
):
    from coppermind_store import indexer

    from coppermind import atomicio

    context, recording, generated, target, payload = await prepare(store)
    before = target.body
    original_write = context.write
    original_drop = indexer.drop_note
    original_update = store.merge_note_update
    original_atomic = atomicio.atomic_write_text
    triggered = False

    def interrupted_write(name, value):
        nonlocal triggered
        original_write(name, value)
        if (
            not triggered
            and name == "recording-merges/" + recording.source_id
            and value.get("step") == crash
        ):
            triggered = True
            raise RuntimeError("simulated crash")
        if (
            not triggered
            and crash == "retire_prepared"
            and name == "retired-recordings/" + generated.id
        ):
            triggered = True
            raise RuntimeError("simulated crash")

    def interrupted_manifest(path, text, *, mode=0o644):
        nonlocal triggered
        original_atomic(path, text, mode=mode)
        if (
            not triggered
            and crash == "manifest_write"
            and path.name == "manifest.json"
            and path.parent.name == recording.source_id
        ):
            triggered = True
            raise RuntimeError("simulated crash")

    async def interrupted_drop(*args):
        nonlocal triggered
        if not triggered and crash == "retired":
            triggered = True
            raise RuntimeError("simulated crash")
        return await original_drop(*args)

    async def interrupted_update(note_id, changes, suffix, if_match):
        nonlocal triggered
        result = await original_update(note_id, changes, suffix, if_match)
        marker = "<!-- coppermind-recording:" + recording.source_id + " -->"
        attach = crash == "attach_write" and recording.source_id in result.sources
        summary = crash == "summary_write" and marker in result.body
        if not triggered and note_id == target.id and (attach or summary):
            triggered = True
            raise RuntimeError("simulated crash")
        return result

    monkeypatch.setattr(context, "write", interrupted_write)
    monkeypatch.setattr(indexer, "drop_note", interrupted_drop)
    monkeypatch.setattr(store, "merge_note_update", interrupted_update)
    monkeypatch.setattr(atomicio, "atomic_write_text", interrupted_manifest)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await context.lifecycle.merge_recording(payload)
    assert triggered
    monkeypatch.setattr(context, "write", original_write)
    monkeypatch.setattr(indexer, "drop_note", original_drop)
    monkeypatch.setattr(store, "merge_note_update", original_update)
    monkeypatch.setattr(atomicio, "atomic_write_text", original_atomic)
    if crash == "attached":
        current = await store.get_note(target.id)
        await store.replace_note(
            target.id,
            ReplaceNote(
                frontmatter=current.frontmatter, body=current.body + "Owner edit while waiting.\n"
            ),
            current.content_hash,
        )
    await context.lifecycle.resume_recording_merges()
    receipt = context.read("recording-merges/" + recording.source_id, {})
    assert receipt["state"] == "complete", receipt.get("error")
    result = await store.get_note(target.id)
    assert result.body.startswith(before)
    assert result.body.count("## Recording summary") == 1
    assert "We agreed to ship." in result.body
    assert "Transcript]]" in result.body
    if crash == "attached":
        assert "Owner edit while waiting." in result.body
    assert recording.source_id in result.sources
    assert recording.artifact_id in result.frontmatter["artifacts"]
    await reconcile_once(store)
    from coppermind.store_protocol import NoteQuery

    page = await store.list_notes(NoteQuery(state="deleted"))
    assert generated.id in {item.id for item in page.items}
    manifest = await store.get_source(recording.source_id)
    assert manifest.current_revision == 1
    revision = manifest.revisions[0]
    replay_artifacts = []
    for artifact in revision.artifacts:
        document = await store.get_source_artifact(recording.source_id, 1, artifact.name)
        replay_artifacts.append(
            IngestArtifact(
                name=artifact.name, mime_type=artifact.mime_type, content=document.content
            )
        )
    replay = await store.ingest(
        IngestRequest(
            source=IngestSource(
                provider=manifest.provider,
                external_source_id=manifest.external_source_id,
                source_type=manifest.source_type,
                origin=manifest.origin,
                captured_at=revision.captured_at,
                metadata=revision.metadata,
                artifacts=replay_artifacts,
            ),
            note=IngestNote(title="Retry"),
        )
    )
    assert replay.note.id == target.id
    await context.recordings.file_note(
        recording,
        {"segments": [{"start": 0, "speaker": "Speaker 1", "text": "Hello Casey."}]},
    )
    assert recording.note_id == generated.id
    assert (await context.recordings.detail({"id": recording.id}))["note_id"] == target.id
    assert (await context.recordings.status({"id": recording.id}))["note_id"] == target.id
    await context.lifecycle.resume_recording_merges()
    assert (await store.get_note(target.id)).body == result.body


async def test_unavailable_destination_keeps_recording_and_reports_problem(store, monkeypatch):
    context, recording, generated, target, payload = await prepare(store)
    original_write = context.write

    def interrupt(name, value):
        original_write(name, value)
        if name == "recording-merges/" + recording.source_id and value.get("step") == "attached":
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(context, "write", interrupt)
    with pytest.raises(RuntimeError):
        await context.lifecycle.merge_recording(payload)
    monkeypatch.setattr(context, "write", original_write)
    target_path = (await store._locate(target.id))[1]
    saved_target = target_path.read_bytes()
    target_path.unlink()
    await context.lifecycle.resume_recording_merges()
    receipt = context.read("recording-merges/" + recording.source_id, {})
    assert receipt["state"] == "attention"
    assert "unavailable" in receipt["error"].lower()
    assert (await store.get_note(generated.id)).id == generated.id
    target_path.write_bytes(saved_target)
    retried = await context.lifecycle.retry_recording_merge(
        {"note_id": generated.id, "if_match": generated.content_hash}
    )
    assert retried["state"] == "complete" and retried["target_id"] == target.id


async def test_dismissal_is_remembered_per_recording(store):
    context, recording, generated, target, payload = await prepare(store)
    assert [c["id"] for c in await context.lifecycle.merge_candidates(generated, recording)] == [
        target.id
    ]
    assert await context.lifecycle.dismiss_recording_merge(
        {"note_id": generated.id, "if_match": generated.content_hash}
    ) == {"dismissed": True}
    assert await context.lifecycle.merge_candidates(generated, recording) == []
    assert await context.lifecycle.dismiss_recording_merge(
        {"note_id": generated.id, "if_match": generated.content_hash}
    ) == {"dismissed": True}


async def test_companion_api_proposes_and_confirms_event_match(store):
    import httpx
    from coppermind_api.auth import Principal
    from coppermind_api.deps import authenticated_key
    from coppermind_api.deps import store as store_dependency
    from coppermind_api.v1.workflow import router
    from fastapi import FastAPI

    context, recording, generated, target, payload = await prepare(store)

    class Client:
        async def context_action(self, action, data):
            if action == "lifecycle":
                return await context.lifecycle_detail(data)
            if action == "merge-recording":
                return await context.lifecycle.merge_recording(data)
            raise AssertionError(action)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[store_dependency] = lambda: Client()
    app.dependency_overrides[authenticated_key] = lambda: Principal(
        "test",
        frozenset({"notes:read", "notes:write", "sources:read", "sources:write", "entities:read"}),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local.test"
    ) as client:
        review = await client.get(f"/v1/notes/{generated.id}/review")
        assert review.status_code == 200
        assert [c["id"] for c in review.json()["recording_merge"]["candidates"]] == [target.id]
        result = await client.post(
            f"/v1/notes/{generated.id}/recording-merge",
            json={k: v for k, v in payload.items() if k != "note_id"},
        )
        assert result.status_code == 200
        assert result.json() == {"state": "complete", "target_id": target.id}
    note = await store.get_note(target.id)
    assert "My own words." in note.body and "We agreed to ship." in note.body
    assert recording.source_id in note.sources


async def test_selecting_a_meeting_updates_generated_title_and_keeps_merge_available(
    store, monkeypatch
):
    context, recording, generated, target, payload = await prepare(store)
    candidates = await context.recording_calendar_candidates(generated)
    assert candidates
    selected = {**candidates[0], "title": "Updated planning"}

    async def candidates_for_note(note):
        return [selected]

    monkeypatch.setattr(context, "recording_calendar_candidates", candidates_for_note)
    await context.lifecycle.choose_recording_meeting(
        {
            "note_id": generated.id,
            "if_match": generated.content_hash,
            "calendar_hash": await context.calendar_evidence_hash(generated),
            "event_id": selected["event_id"],
        }
    )
    updated = await store.get_note(generated.id)
    assert updated.body.startswith("# Updated planning\n")
    assert updated.path == generated.path
    assert list((context.root / "recording-meeting-history" / recording.source_id).glob("*.json"))
    job = context.newest_job(updated.id)
    job.result = Suggestions(
        meeting_summary=MeetingSummary(summary="We agreed to ship.", decisions="", action_items="")
    )
    job.state = "suggested"
    context.save_job(job)
    payload.update(if_match=updated.content_hash, job_id=job.id)
    result = await context.lifecycle.merge_recording(payload)
    assert result == {"state": "complete", "target_id": target.id}


async def test_merge_preserves_owner_frontmatter_comments_and_body(store):
    context, recording, generated, target, payload = await prepare(store)
    path = (await store._locate(target.id))[1]
    raw = path.read_text()
    path.write_text(raw.replace("---\n", "---\n# My hand-written comment\n", 1).rstrip("\n"))
    await reconcile_once(store)
    target = await store.get_note(target.id)
    payload["target_hash"] = target.content_hash
    before_body = target.body
    await context.lifecycle.merge_recording(payload)
    after = path.read_text()
    assert "# My hand-written comment\n" in after
    assert (await store.get_note(target.id)).body.startswith(before_body)
