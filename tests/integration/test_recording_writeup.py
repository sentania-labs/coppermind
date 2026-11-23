"""Synthetic audio -> transcript -> Markdown -> real ingest and independent review."""

from __future__ import annotations

import asyncio
import io
import json
import os
import wave
from pathlib import Path

import pytest
import sqlalchemy as sa
from coppermind_store.model_provider import ProviderError
from coppermind_store.voice_store import VoiceStore

from coppermind.db.models import Note
from coppermind.entities import EntityInput
from coppermind.store_protocol import ReplaceNote
from services.store.tests.test_transcription_call import REPLY, Service
from tests.integration.test_recordings import request, setup_transcription, store_audio


async def count_notes(store):
    async with store.session_factory() as session:
        return await session.scalar(sa.select(sa.func.count()).select_from(Note))


async def test_finished_body_without_suggestions_then_review(store):
    vector = {"model": "synthetic", "version": "1", "vector": [1.0, 0.0]}
    other = {"model": "synthetic", "version": "1", "vector": [0.0, 1.0]}
    async with Service(
        reply={
            **REPLY,
            "speakers": [
                {"label": "Speaker 1", "speech_seconds": 30, "embeddings": [vector]},
                {"label": "Speaker 2", "speech_seconds": 30, "embeddings": [other]},
            ],
        }
    ) as service:
        context = await setup_transcription(store, service)
        person = await store.entities.create(EntityInput(kind="person", name="Alex Example"))
        VoiceStore(context).confirm(person["id"], "synthetic-previous", "2026-11-19", 30, [vector])
        assert not context.config().assignments["enrichment"].enabled
        evidence_seen = []

        async def write_text(connection, key, limits, system, evidence):
            evidence_seen.append(evidence)
            assert evidence["allowed_speaker_labels"] == ["Alex Example", "Unknown voice 1"]
            return (
                "# " + evidence["resolved_title"] + "\n\n## Top line\nA brief greeting.\n\n"
                "## Action items\nNo explicit commitments.\n\n## Executive summary\n"
                "Alex Example and Unknown voice 1 greeted each other.\n"
                + "\n<<<RECORDING_BOUNDARIES>>>\n[]\n<<<END_RECORDING_BOUNDARIES>>>\n"
            ), {}

        context.provider.write_text = write_text
        recording = io.BytesIO()
        with wave.open(recording, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\x00\x00" * 8000 * 6)
        audio_bytes = recording.getvalue()
        audio = await store_audio(store, name="synthetic.wav", data=audio_bytes, mime="audio/wav")
        if proof := os.environ.get("RECORDING_PROOF_DIR"):
            Path(proof, "synthetic.wav").write_bytes(audio_bytes)
        accepted = await context.accept_recording(request(audio))
        assert context.recordings.load(accepted["id"]).generation_mode == "markdown-v1"
        await context.recordings.tick()
        job = context.recordings.load(accepted["id"])
        assert job.state == "succeeded", job.error
        assert job.stage == "Note ready"
        note = await store.get_note(job.note_id)
        assert note.body.count("# 2026-11-09 Standup\n") == 1
        assert "Alex Example and Unknown voice 1" in note.body
        assert note.frontmatter["reviewed"] is False and note.path.startswith("Review/")
        assert note.path.count("2026-11-09") == 1
        assert not context.read("meeting-summaries/" + note.id, {})
        assert (await store.admin.notes.summary({"id": note.id})) == {"recording": False}
        await context.setup_lifecycle({"revision": context.config().revision})
        before = note.body
        from coppermind_store import speaker_names

        evidence = await speaker_names.evidence(context, note, [])
        assert evidence["speakers"] == ["Unknown voice 1"]
        assert not any(c["name"] == "Alex Example" for c in evidence["candidates"])
        voices = VoiceStore(context).unknown_for_note(note.id)
        assert len(voices) == 1 and voices[0]["name"] == "Unknown voice 1"
        assert store.admin.incoming.recording(job)["unnamed_voices"] == voices
        assert (await store.admin.notes.detail({"id": note.id}))["unnamed_voices"] == voices
        people = await store.admin.people.listing()
        assert people["unnamed"][0]["label"] == "Unknown voice 1"
        config = context.config()
        enrichment = config.assignments["enrichment"]
        enrichment.connection = config.assignments["writeup"].connection
        enrichment.model = "synthetic-suggestions"
        enrichment.enabled = True
        enrichment.fields = ["type"]
        context.save_config(config)
        context.write("discovery", {enrichment.connection: [enrichment.model]})

        async def suggest(*args):
            return {
                "suggestions": [
                    {
                        "kind": "field",
                        "field": "type",
                        "value": "reference",
                        "confidence": 0.9,
                        "reason": "A brief greeting for reference.",
                        "quote": "A brief greeting.",
                    }
                ],
                "unresolved": [],
            }, {}

        context.provider.suggest = suggest
        review = context.load_job(await context.queue_note(note.id))
        await context.run_job(review)
        assert review.state == "suggested", review.error
        assert review.result.meeting_summary is None
        # A cached suggestion made before a voice was named must not offer that same person
        # as the remaining unknown speaker.
        from coppermind_store.enrichment_models import Suggestion

        candidate = speaker_names.person_candidate(store.entities.load().entities[person["id"]])
        review.evidence["speaker_review"] = {**evidence, "candidates": [candidate]}
        review.result.suggestions.append(
            Suggestion(
                kind="speaker",
                field="Unknown voice 1",
                value=candidate["id"],
                confidence=0.95,
                reason="A cached guess.",
                quote="Hello.",
            )
        )
        context.save_job(review)
        detail = await context.lifecycle.detail({"note_id": note.id})
        speaker = next(s for s in detail["suggestions"] if s["kind"] == "speaker")
        assert speaker["value"] is None and speaker["candidates"] == []
        assert detail["unnamed_voices"] == voices
        selected = [
            i
            for i, suggestion in enumerate(review.result.suggestions)
            if suggestion.field == "type"
        ]
        assert selected
        await context.complete_review(
            {
                "note_id": note.id,
                "if_match": note.content_hash,
                "selected": selected,
                "job_id": review.id,
                "confirmation": "Complete review",
            }
        )
        reviewed = await store.get_note(note.id)
        assert reviewed.body == before
        assert reviewed.frontmatter["reviewed"] is True
        assert reviewed.frontmatter["type"] == "reference"
        proof = os.environ.get("RECORDING_PROOF_DIR")
        if proof:
            root = Path(proof)
            (root / "finished-note.md").write_text(before)
            (root / "ready.json").write_text(
                json.dumps(await context.recordings.waiting(), indent=2)
            )
        assert len(service.requests) == 1 and len(evidence_seen) == 1


async def test_failed_writeup_retains_transcript_retry_and_crash_replay(store, monkeypatch):
    async with Service() as service:
        context = await setup_transcription(store, service)
        good_writer = context.provider.write_text

        async def fail(*args, **kwargs):
            raise ProviderError("synthetic cut-short", "provider-incomplete", cut_short=True)

        context.provider.write_text = fail
        audio = await store_audio(store)
        accepted = await context.accept_recording(request(audio))
        await context.recordings.tick()
        job = context.recordings.load(accepted["id"])
        assert job.state == "failed" and job.stage == "Writing up"
        assert job.transcribed and not job.note_id and await count_notes(store) == 0
        assert not job.retry_after
        waiting = await context.recordings.waiting()
        assert waiting["failed"][0]["stage"] == "Writing up"
        assert waiting["failed"][0]["can_retry"] is True
        proof = os.environ.get("RECORDING_PROOF_DIR")
        if proof:
            Path(proof, "failed.json").write_text(json.dumps(waiting, indent=2))
        context.provider.write_text = good_writer
        await context.recordings.retry({"id": job.id})
        original = store.ingest

        async def crash_after_ingest(*args, **kwargs):
            await original(*args, **kwargs)
            raise asyncio.CancelledError()

        monkeypatch.setattr(store, "ingest", crash_after_ingest)
        with pytest.raises(asyncio.CancelledError):
            await context.recordings.tick()
        job = context.recordings.load(job.id)
        assert job.stage == "Saving note" and await count_notes(store) == 1
        saved = context.read("recording-writeups/" + job.id, {})
        assert saved["body"]
        monkeypatch.setattr(store, "ingest", original)
        context.provider.write_text = fail  # Replay must not call the model again.
        await context.recordings.tick()
        job = context.recordings.load(job.id)
        assert job.state == "succeeded", job.error
        assert await count_notes(store) == 1 and len(service.requests) == 1


async def test_new_generated_voice_lines_relabel_without_overwriting_owner_edits(store):
    from coppermind_store.voice_relabel import relabel_recording

    vector = {"model": "synthetic", "version": "1", "vector": [1.0, 0.0]}
    async with Service(
        reply={
            **REPLY,
            "speakers": [
                {"label": "Speaker 1", "speech_seconds": 30, "embeddings": [vector]},
            ],
        }
    ) as service:
        context = await setup_transcription(store, service)

        async def write_text(connection, key, limits, system, evidence):
            return (
                "# " + evidence["resolved_title"] + "\n\nUnknown voice 1 greeted the team.\n"
                "\n<<<RECORDING_BOUNDARIES>>>\n[]\n<<<END_RECORDING_BOUNDARIES>>>\n",
                {},
            )

        context.provider.write_text = write_text
        accepted = await context.accept_recording(request(await store_audio(store)))
        await context.recordings.tick()
        job = context.recordings.load(accepted["id"])
        note = await store.get_note(job.note_id)
        await store.replace_note(
            note.id,
            ReplaceNote(
                frontmatter=note.frontmatter,
                body=note.body + "\nOwner mentions Unknown voice 1 here.\n",
            ),
            if_match=note.content_hash,
        )
        appearance = VoiceStore(context).assignments("unknown:1")[0]
        await relabel_recording(context, appearance, "Unknown voice 1", "Alex Example")
        note = await store.get_note(note.id)
        assert "Alex Example greeted the team." in note.body
        assert "Owner mentions Unknown voice 1 here." in note.body


async def test_boundary_metadata_survives_replay_and_is_exposed_in_status(store):
    boundary = {
        "start_seconds": 3.4,
        "end_seconds": 5.2,
        "kind": "unrelated_tail",
        "evidence": "The speaker says goodbye before a separate greeting.",
        "confidence": "low",
    }
    async with Service() as service:
        context = await setup_transcription(store, service)

        async def writer(connection, key, limits, system, evidence):
            return (
                "# " + evidence["resolved_title"] + "\n\n## Top line\nA greeting.\n\n"
                "## Recording boundaries\n00:00:03.4 to 00:00:05.2 may be unrelated tail.\n"
                "\n<<<RECORDING_BOUNDARIES>>>\n"
                + json.dumps([boundary])
                + "\n<<<END_RECORDING_BOUNDARIES>>>\n"
            ), {}

        context.provider.write_text = writer
        accepted = await context.accept_recording(request(await store_audio(store)))
        await context.recordings.tick()
        status = await context.recording_status({"id": accepted["id"]})
        assert status["state"] == "succeeded", status["error"]
        assert status["boundaries"] == [boundary]
        import httpx
        from coppermind_store.auth import InternalAuth
        from coppermind_store.context_api import router as context_router
        from fastapi import FastAPI

        from coppermind.store_client import HttpStoreClient
        from tests.integration.test_store_write_protocol import authorized_api

        internal = FastAPI()
        internal.state.store = store
        internal.state.auth = InternalAuth("synthetic-internal-token")
        internal.include_router(context_router)
        app, headers = authorized_api(store)
        app.state.store = HttpStoreClient(
            "http://store", "synthetic-internal-token", transport=httpx.ASGITransport(app=internal)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api", headers=headers
        ) as api:
            response = await api.get("/v1/recordings/" + accepted["id"])
            assert response.status_code == 200
            assert response.json()["boundaries"] == [boundary]
            waiting = await api.get("/v1/recordings/waiting")
            assert waiting.json()["recordings"][0]["boundaries"] == [boundary]
        note = await store.get_note(status["note_id"])
        assert "Recording boundaries" in note.body and "<<<" not in note.body
        assert "start_seconds" not in note.body
        job = context.recordings.load(accepted["id"])
        job.state = "running"
        context.recordings.save(job)
        await context.recordings.tick()
        assert (await context.recording_status({"id": job.id}))["boundaries"] == [boundary]
        assert await count_notes(store) == 1
        proof = os.environ.get("RECORDING_PROOF_DIR")
        if proof:
            Path(proof, "boundaries.json").write_text(json.dumps(status, indent=2))


BOUNDARY_BLOCK = "\n<<<RECORDING_BOUNDARIES>>>\n[]\n<<<END_RECORDING_BOUNDARIES>>>\n"


async def transcribed(store, service, segments, *, limit=None):
    """A recording whose transcript is saved, so only the write-up and filing remain."""
    context = await setup_transcription(store, service)
    accepted = await context.accept_recording(request(await store_audio(store)))
    job = context.recordings.load(accepted["id"])
    context.write(context.recordings.transcript_path(job), {"segments": segments})
    job.transcribed = True
    context.recordings.save(job)
    if limit:
        config = context.config()
        config.assignments["writeup"].summary_max_input_chars = limit
        context.save_config(config)
    return context, job


def turns(count, words=40):
    return [
        {"speaker": "Speaker 1", "start": n * 10, "end": n * 10 + 9, "text": f"turn{n} " * words}
        for n in range(count)
    ]


async def test_long_transcript_is_written_up_in_parts_and_combined(store):
    async with Service() as service:
        # About 90,000 characters against a limit of 6,000.
        context, job = await transcribed(store, service, turns(300, 40), limit=6000)
        calls = []

        async def write_text(connection, key, limits, system, evidence, **kwargs):
            calls.append((system, evidence))
            assert len(json.dumps(evidence, ensure_ascii=False)) <= 6000
            if "part_notes" in evidence:
                assert "transcript" not in evidence
                return (
                    "# "
                    + evidence["resolved_title"]
                    + "\n\n## Top line\nWhole recording, "
                    + str(len(evidence["part_notes"]))
                    + " parts combined.\n"
                    + BOUNDARY_BLOCK
                ), {"total_tokens": 7}
            return f"- Covered {evidence['covers']}.", {"total_tokens": 3}

        context.provider.write_text = write_text
        await context.recordings.tick()
        job = context.recordings.load(job.id)
        assert job.state == "succeeded", job.error
        parts = [c for c in calls if "part_notes" not in c[1]]
        final = [c for c in calls if "part_notes" in c[1]]
        assert len(final) == 1 and len(parts) >= 15
        assert len(final[0][1]["part_notes"]) == len(parts)
        # Every word of the transcript was sent in exactly one part.
        sent = "".join(c[1]["transcript"] for c in parts)
        assert all(f"turn{n} " in sent for n in range(300))
        assert sent.count("turn299 ") == 40
        note = await store.get_note(job.note_id)
        assert "parts combined" in note.body
        saved = context.read("recording-writeups/" + job.id, {})
        assert saved["parts"] == len(parts)
        assert saved["usage"]["total_tokens"] == 3 * len(parts) + 7
        assert not context.read("recording-writeup-parts/" + job.id, {})


async def test_a_retry_after_failure_does_not_write_the_finished_parts_again(store):
    async with Service() as service:
        context, job = await transcribed(store, service, turns(60, 40), limit=6000)
        calls = []
        broken = {"final": True}

        async def write_text(connection, key, limits, system, evidence, **kwargs):
            calls.append(evidence)
            if "part_notes" in evidence:
                if broken["final"]:
                    raise ProviderError("synthetic", "provider-dropped")
                return "# " + evidence["resolved_title"] + "\n\n## Top line\nDone.\n" + (
                    BOUNDARY_BLOCK
                ), {}
            return f"- {evidence['covers']}", {}

        context.provider.write_text = write_text
        await context.recordings.tick()
        assert context.recordings.load(job.id).state == "failed"
        first = len(calls)
        broken["final"] = False
        await context.recordings.retry({"id": job.id})
        await context.recordings.tick()
        assert context.recordings.load(job.id).state == "succeeded"
        assert len(calls) == first + 1


async def test_notes_that_cannot_be_made_to_fit_fail_with_the_limit_and_no_note(store):
    async with Service() as service:
        context, job = await transcribed(store, service, turns(30, 40), limit=3000)

        async def write_text(connection, key, limits, system, evidence, **kwargs):
            return "- " + "long " * 500, {}

        context.provider.write_text = write_text
        await context.recordings.tick()
        job = context.recordings.load(job.id)
        assert job.state == "failed" and job.error_rule == "writeup-input-limit"
        assert "in parts" in job.error and not job.note_id and await count_notes(store) == 0


async def test_a_loose_reply_still_files_the_note(store):
    async with Service() as service:
        context, job = await transcribed(store, service, turns(3))

        async def write_text(connection, key, limits, system, evidence, **kwargs):
            title = evidence["resolved_title"]
            return (
                "Sure, here is the note:\n\n```markdown\n# "
                + title
                + "\n\n## Top line\nShip it \u2014 if QA agrees.\n\n---\n\n## Executive summary\n"
                "Calls <10 minutes.\n```"
            ), {}

        context.provider.write_text = write_text
        await context.recordings.tick()
        job = context.recordings.load(job.id)
        assert job.state == "succeeded", job.error
        note = await store.get_note(job.note_id)
        assert "\u2014" not in note.body and "```" not in note.body and "<10 minutes" in note.body
        saved = context.read("recording-writeups/" + job.id, {})
        assert "replaced em-dashes" in saved["repairs"]
        status = await context.recording_status({"id": job.id})
        assert status["summary_state"] == "succeeded"


async def test_a_refused_note_is_asked_for_again_with_what_was_wrong(store):
    async with Service() as service:
        context, job = await transcribed(store, service, turns(3))
        asked = []

        async def write_text(connection, key, limits, system, evidence, **kwargs):
            asked.append(kwargs.get("correction"))
            if not kwargs.get("correction"):
                return (
                    "# "
                    + evidence["resolved_title"]
                    + "\n\n## Action items\n### Zed\nDo it.\n"
                    + BOUNDARY_BLOCK
                ), {}
            return (
                "# " + evidence["resolved_title"] + "\n\n## Top line\nDone.\n" + BOUNDARY_BLOCK
            ), {}

        context.provider.write_text = write_text
        await context.recordings.tick()
        job = context.recordings.load(job.id)
        assert job.state == "succeeded", job.error
        assert asked[0] is None and len(asked) == 2
        second = asked[1]
        assert second is not None
        assert "speaker that was not supplied" in second[1] and "Zed" not in second[1]


async def test_a_note_refused_twice_says_what_was_wrong_without_repeating_the_model(store):
    async with Service() as service:
        context, job = await transcribed(store, service, turns(3))

        async def write_text(connection, key, limits, system, evidence, **kwargs):
            return (
                "# "
                + evidence["resolved_title"]
                + "\n\n## Action items\n### Zed\nDo it.\n"
                + BOUNDARY_BLOCK
            ), {}

        context.provider.write_text = write_text
        await context.recordings.tick()
        job = context.recordings.load(job.id)
        assert job.state == "failed" and job.error_rule == "writeup-invalid"
        assert "could not be used as a complete note" in job.display_error
        assert "speaker that was not supplied" in job.display_error
        assert "Zed" not in job.display_error and "Zed" in job.reply
        assert not job.note_id and await count_notes(store) == 0
        status = await context.recording_status({"id": job.id})
        assert status["summary_state"] == "failed"
        assert status["summary_error_rule"] == "writeup-invalid"
        assert status["summary_attempts"] == 1 and status["summary_finished_at"]
        assert status["state"] == "failed"


async def test_naming_during_review_finishes_then_relabels_generated_lines(store):
    from coppermind.entities import EntityInput

    vector = {"model": "synthetic", "version": "1", "vector": [1.0, 0.0]}
    async with Service(
        reply={
            **REPLY,
            "speakers": [
                {"label": "Speaker 1", "speech_seconds": 30, "embeddings": [vector]},
            ],
        }
    ) as service:
        context = await setup_transcription(store, service)
        await context.setup_lifecycle({"revision": context.config().revision})
        person = await store.entities.create(EntityInput(kind="person", name="Alex Example"))
        other = await store.entities.create(EntityInput(kind="person", name="Blair Example"))

        async def writer(connection, key, limits, system, evidence):
            return (
                "# " + evidence["resolved_title"] + "\n\nUnknown voice 1 said hello.\n"
                "Unknown voice 2 (recording-local) answered.\n"
                "<<<RECORDING_BOUNDARIES>>>\n[]\n<<<END_RECORDING_BOUNDARIES>>>"
            ), {}

        context.provider.write_text = writer
        accepted = await context.accept_recording(request(await store_audio(store)))
        await context.recordings.tick()
        job = context.recordings.load(accepted["id"])
        assert job.state == "succeeded", job.error
        note = await store.get_note(job.note_id)
        detail = await context.lifecycle_detail({"note_id": note.id})
        manual = detail["manual_speaker_review"]
        await context.complete_review(
            {
                "note_id": note.id,
                "if_match": note.content_hash,
                "selected": [],
                "confirmation": "Complete review",
                "manual_source_id": manual["source_id"],
                "manual_source_revision": manual["revision"],
                "manual_speakers": {
                    "Unknown voice 1": "person:" + person["id"],
                    "Unknown voice 2 (recording-local)": "person:" + other["id"],
                },
            }
        )
        note = await store.get_note(note.id)
        assert note.frontmatter["reviewed"] is True
        assert "Alex Example said hello." in note.body
        assert "Blair Example answered." in note.body


async def test_voice_named_during_generation_is_applied_after_durable_filing(store):
    async with Service() as service:
        context = await setup_transcription(store, service)
        writer = context.provider.write_text
        accepted = await context.accept_recording(request(await store_audio(store)))

        async def write_text(*args):
            document, usage = await writer(*args)
            document = document.replace(
                "The speakers greeted each other.",
                "Unknown voice 1 (recording-local) greeted the team.",
            )
            mapping = context.read("voice-mappings/" + accepted["id"], {})
            mapping["Speaker 1"] = "Alex Example"
            context.write("voice-mappings/" + accepted["id"], mapping)
            return document, usage

        context.provider.write_text = write_text
        await context.recordings.tick()
        job = context.recordings.load(accepted["id"])
        assert job.state == "succeeded", job.error
        saved = context.read("recording-writeups/" + job.id, {})
        assert saved["voice_map"]["Speaker 1"] == "Unknown voice 1 (recording-local)"
        manifest = await store.get_source(job.source_id)
        artifact = await store.get_source_artifact(
            job.source_id, manifest.current_revision, "transcript.json"
        )
        assert '"speaker": "Alex Example"' in artifact.content
        note = await store.get_note(job.note_id)
        assert "Alex Example" in note.body
        # A replay after that explicit correction must retain the corrected source and body.
        await context.recordings.file_note(job, REPLY)
        assert (await store.get_source(job.source_id)).current_revision == manifest.current_revision
        assert (await store.get_note(job.note_id)).body == note.body
        assert await count_notes(store) == 1


async def test_writeup_editor_persists_and_resets_only_its_prompt(store):
    from coppermind.recording_writeup import DEFAULT_PROMPT, DISPLAY_RULES

    async with Service() as service:
        context = await setup_transcription(store, service)
        features = store.admin.features
        before = context.config().assignments["enrichment"].model_dump()
        await features.writeup_save({"writeup_prompt": "Write a concise account.", "enabled": True})
        settings = await features.writeup_settings()
        assert settings["prompt"] == "Write a concise account."
        assert settings["locked"] == DISPLAY_RULES
        await features.writeup_save({"reset": True})
        assert (await features.writeup_settings())["prompt"] == DEFAULT_PROMPT
        assert context.config().assignments["enrichment"].model_dump() == before
