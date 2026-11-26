"""Synthetic audio -> transcript -> Markdown -> real ingest and independent review."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
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
        # Speakers first, then the top line; no title heading; one references block last.
        assert "# 2026-11-09 Standup" not in note.body
        speakers = note.body.partition("## Top line")[0]
        assert speakers.startswith("## Speakers\n\n")
        assert re.search(
            r"^- (\[\[[^\]|]+\|)?Alex Example(\]\])?, 3 s. Identified by voice \(strong match\)\.$",
            speakers,
            re.M,
        )
        assert "- Unknown voice 1, 4 s. Not identified yet.\n" in speakers
        assert "  - [ ] This is me\n  - Name this voice:" in speakers
        assert speakers.index("Alex Example") < speakers.index("Unknown voice 1")
        assert "Alex Example and Unknown voice 1" in note.body
        references = note.body.partition("## References")[2]
        assert note.body.count("## References") == 1 and "## Artifacts" not in note.body
        assert "- Source transcript: [[" in references and "- Recording: [[" in references
        assert "- Calendar: not matched" in references
        assert "## Recording boundaries" not in note.body
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
        # Heard and recognised by voice, so the write-up suggested them as an attendee, and
        # the confident suggestion filled the blank field the way any other does.
        assert any(
            s.field == "attendees" and s.origin == "writeup" for s in review.result.suggestions
        )
        note = await store.get_note(note.id)
        assert note.frontmatter["attendees"] == ["[[People/Alex Example]]"]
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
                "Calls <9 minutes.\n```"
            ), {}

        context.provider.write_text = write_text
        await context.recordings.tick()
        job = context.recordings.load(job.id)
        assert job.state == "succeeded", job.error
        note = await store.get_note(job.note_id)
        assert "\u2014" not in note.body and "```" not in note.body and "<9 minutes" in note.body
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


async def test_a_reply_that_is_not_a_note_fails_with_try_again_and_is_never_filed(store):
    async with Service() as service:
        context, job = await transcribed(store, service, turns(3))
        asked = []

        async def refuse(connection, key, limits, system, evidence, **kwargs):
            asked.append(kwargs.get("correction"))
            return "I could not produce a note for this recording.", {}

        context.provider.write_text = refuse
        await context.recordings.tick()
        job = context.recordings.load(job.id)
        assert job.state == "failed" and job.error_rule == "writeup-invalid"
        assert len(asked) == 2 and asked[0] is None
        assert asked[1] is not None and "not a note" in asked[1][1]
        assert not job.note_id and await count_notes(store) == 0
        assert not context.read("recording-writeups/" + job.id, {}).get("body")
        status = await context.recording_status({"id": job.id})
        assert status["summary_state"] == "failed" and status["state"] == "failed"
        failed = (await context.recordings.waiting())["failed"]
        assert failed[0]["can_retry"] is True and "could not produce" not in failed[0]["message"]

        async def good(connection, key, limits, system, evidence, **kwargs):
            return (
                "# " + evidence["resolved_title"] + "\n\n## Top line\nDone.\n" + BOUNDARY_BLOCK
            ), {}

        context.provider.write_text = good
        await context.recordings.retry({"id": job.id})
        await context.recordings.tick()
        job = context.recordings.load(job.id)
        assert job.state == "succeeded", job.error
        assert "Done." in (await store.get_note(job.note_id)).body


async def test_what_the_recording_does_not_say_is_marked_in_the_filed_note(store):
    segments = [
        {"speaker": "Speaker 1", "start": 0, "end": 9, "text": "We run vSphere 7 on forty hosts."},
        {"speaker": "Speaker 1", "start": 10, "end": 19, "text": "Half are reimaged already."},
    ]
    async with Service() as service:
        context, job = await transcribed(store, service, segments)

        async def write_text(connection, key, limits, system, evidence, **kwargs):
            return (
                "# " + evidence["resolved_title"] + "\n\n## Top line\n"
                "About 40 hosts, and a $250,000 budget agreed with Contoso.\n" + BOUNDARY_BLOCK
            ), {}

        context.provider.write_text = write_text
        await context.recordings.tick()
        job = context.recordings.load(job.id)
        assert job.state == "succeeded", job.error
        body = (await store.get_note(job.note_id)).body
        assert "About 40 hosts, and a $250,000 (not in the recording) budget" in body
        assert "agreed with Contoso (not in the recording)." in body
        assert "About 40 (not in the recording)" not in body
        saved = context.read("recording-writeups/" + job.id, {})
        assert saved["unsupported"] == ["$250,000", "Contoso"]
        # The reply as the model wrote it is kept as it was.
        assert "$250,000 budget" in saved["document"]


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


async def test_part_notes_that_fit_alone_but_not_together_are_condensed_first(store):
    async with Service() as service:
        context, job = await transcribed(store, service, turns(30, 40), limit=3000)
        calls = []

        async def write_text(connection, key, limits, system, evidence, **kwargs):
            calls.append(evidence)
            if "transcript" in evidence:
                return "- " + "detail " * 200, {}  # about 1,400 characters: two do not fit
            if "part_notes" in evidence and "resolved_title" not in evidence:
                return "- short", {}  # a part note condensed
            return (
                "# " + evidence["resolved_title"] + "\n\n## Top line\nDone.\n" + BOUNDARY_BLOCK
            ), {}

        context.provider.write_text = write_text
        await context.recordings.tick()
        job = context.recordings.load(job.id)
        assert job.state == "succeeded", job.error
        condensed = [c for c in calls if "part_notes" in c and "resolved_title" not in c]
        assert condensed and all(len(c["part_notes"]) == 1 for c in condensed[:1])
        assert any("resolved_title" in c and "part_notes" in c for c in calls)


CUSTOMER = {
    **REPLY,
    "duration": 40.0,
    "segments": [
        {
            "id": "s1",
            "start": 0.0,
            "end": 9.0,
            "text": "Halcyon runs Orbit 7.2 and wants to reach 8.0 before March.",
            "speaker": "Speaker 1",
        },
        {
            "id": "s2",
            "start": 10.0,
            "end": 20.0,
            "text": "Halcyon's cost here is trivial next to our data centers.",
            "speaker": "Speaker 2",
        },
    ],
}


def suggestion_block(items):
    return (
        "<<<FIELD_SUGGESTIONS>>>\n"
        + json.dumps({"suggestions": items})
        + "\n<<<END_FIELD_SUGGESTIONS>>>\n"
    )


async def customer_recording(store, service, answer):
    context = await setup_transcription(store, service)
    await store.entities.create(
        EntityInput(kind="company", name="Halcyon Systems", aliases=["Halcyon"])
    )
    config = context.config()
    config.assignments["writeup"].writeup_glossary = "orbit seven = Orbit 7"
    config.assignments["enrichment"].connection = config.assignments["writeup"].connection
    config.assignments["enrichment"].model = "synthetic-suggestions"
    config.assignments["enrichment"].enabled = True
    context.save_config(config)
    context.write(
        "discovery", {config.assignments["enrichment"].connection: ["synthetic-suggestions"]}
    )
    seen: dict = {}

    async def write_text(connection, key, limits, system, evidence):
        seen.update(system=system, evidence=evidence)
        return answer(evidence), {}

    context.provider.write_text = write_text
    audio = await store_audio(store, name="call.wav", data=b"RIFF....WAVEfmt ", mime="audio/wav")
    accepted = await context.accept_recording(request(audio))
    await context.recordings.tick()
    return context, context.recordings.load(accepted["id"]), seen


def note_answer(extra=""):
    def build(evidence):
        return (
            "# " + evidence["resolved_title"] + "\n\n## Top line\nHalcyon plans Orbit 8.0.\n\n"
            "## What we learned about their environment\nOrbit 7.2 today [00:00:00].\n"
            + extra
            + "\n<<<RECORDING_BOUNDARIES>>>\n[]\n<<<END_RECORDING_BOUNDARIES>>>\n"
        )

    return build


async def test_customer_call_arrives_with_suggested_fields_in_the_review_flow(store):
    block = suggestion_block(
        [
            {"field": "account", "value": "halcyon", "confidence": "high", "reason": "named twice"},
            {"field": "tags", "value": ["orbit", "not-a-tag"], "confidence": "high", "reason": "x"},
            {"field": "type", "value": "personal", "confidence": "low", "reason": "unsure"},
        ]
    )
    async with Service(reply=CUSTOMER) as service:
        context, job, seen = await customer_recording(store, service, note_answer("\n" + block))
        assert job.state == "succeeded", job.error
        evidence = seen["evidence"]
        assert evidence["known_accounts"] == ["Halcyon Systems"]
        assert evidence["recording_date"] == "2026-11-09"
        assert evidence["meeting_kind"] == "unknown"
        assert evidence["glossary"] == "orbit seven = Orbit 7"
        assert "Recording boundaries" in seen["system"]
        assert "<<<FIELD_SUGGESTIONS>>>" not in (await store.get_note(job.note_id)).body
        saved = context.read("recording-writeups/" + job.id, {})
        assert saved["suggestion_error"] == ""
        assert {(s["field"], s["confidence"]) for s in saved["suggestions"]} == {
            ("account", 0.9),
            ("context", 0.9),
        }

        async def suggest(*args):
            return {"suggestions": [], "unresolved": []}, {}

        context.provider.suggest = suggest
        review = context.load_job(await context.queue_note(job.note_id))
        await context.run_job(review)
        assert review.state in {"suggested", "applying", "applied"}, review.error
        by_field = {s.field: s for s in review.result.suggestions if s.kind == "field"}
        assert by_field["account"].value == ["Halcyon Systems"]
        assert by_field["context"].value == "external"
        assert by_field["account"].reason == "named twice" and by_field["account"].confidence == 0.9
        note = await store.get_note(job.note_id)
        assert note.frontmatter["account"] == ["Halcyon Systems"]


async def test_a_company_that_does_not_exist_is_offered_as_a_new_account_and_not_written(store):
    block = suggestion_block(
        [{"field": "account", "value": "Brand New Corp", "confidence": "high", "reason": "named"}]
    )
    async with Service(reply=CUSTOMER) as service:
        context, job, _ = await customer_recording(store, service, note_answer("\n" + block))
        saved = context.read("recording-writeups/" + job.id, {})
        account = next(s for s in saved["suggestions"] if s["field"] == "account")
        assert account["new"] and account["confidence"] < 0.8
        assert account["reason"].startswith("New account?")

        async def suggest(*args):
            return {"suggestions": [], "unresolved": []}, {}

        context.provider.suggest = suggest
        review = context.load_job(await context.queue_note(job.note_id))
        await context.run_job(review)
        assert "account" not in (await store.get_note(job.note_id)).frontmatter
        assert any(s.field == "account" for s in review.result.suggestions)


async def test_an_enrichment_account_keeps_the_context_suggestion_and_is_guarded(store):
    block = suggestion_block(
        [
            {"field": "account", "value": "Halcyon", "confidence": "high", "reason": "named"},
            {"field": "context", "value": "external", "confidence": "high", "reason": "customer"},
        ]
    )
    async with Service(reply=CUSTOMER) as service:
        context, job, _ = await customer_recording(store, service, note_answer("\n" + block))

        async def suggest(*args):
            return {
                "suggestions": [
                    {
                        "kind": "field",
                        "field": "account",
                        "value": ["Halcyon Systems"],
                        "confidence": 0.95,
                        "quote": "Halcyon runs Orbit 7.2",
                        "reason": "named",
                    }
                ],
                "unresolved": [],
            }, {}

        context.provider.suggest = suggest
        review = context.load_job(await context.queue_note(job.note_id))
        await context.run_job(review)
        fields = {s.field: s for s in review.result.suggestions if s.kind == "field"}
        assert fields["context"].value == "external"


async def test_an_enrichment_account_that_does_not_exist_is_offered_as_new_not_filled(store):
    async with Service(reply=CUSTOMER) as service:
        context, job, _ = await customer_recording(store, service, note_answer("\n"))

        async def invent(*args):
            return {
                "suggestions": [
                    {
                        "kind": "field",
                        "field": "account",
                        "value": ["Brand New Corp"],
                        "confidence": 0.99,
                        "quote": "Halcyon runs Orbit 7.2",
                        "reason": "named",
                    }
                ],
                "unresolved": [],
            }, {}

        context.provider.suggest = invent
        review = context.load_job(await context.queue_note(job.note_id))
        await context.run_job(review)
        account = next(s for s in review.result.suggestions if s.field == "account")
        assert account.confidence < 0.8 and account.reason.startswith("New account?")
        assert "account" not in (await store.get_note(job.note_id)).frontmatter


async def test_a_suggestion_block_that_cannot_be_read_never_costs_the_note(store):
    async with Service(reply=CUSTOMER) as service:
        context, job, _ = await customer_recording(
            store,
            service,
            note_answer(
                "\n<<<FIELD_SUGGESTIONS>>>\nnot json at all\n<<<END_FIELD_SUGGESTIONS>>>\n"
            ),
        )
        assert job.state == "succeeded", job.error
        note = await store.get_note(job.note_id)
        assert "Halcyon plans Orbit 8.0." in note.body and "not json" not in note.body
        saved = context.read("recording-writeups/" + job.id, {})
        assert saved["suggestions"] == [] and saved["suggestion_error"]


async def test_a_clean_recording_has_no_boundaries_section(store):
    async with Service(reply=CUSTOMER) as service:
        _, job, _ = await customer_recording(
            store,
            service,
            lambda evidence: (
                "# " + evidence["resolved_title"] + "\n\n## Top line\nDone.\n\n"
                "## Recording boundaries\nNothing was found.\n\n## Notes\nBody.\n"
                "\n<<<RECORDING_BOUNDARIES>>>\n[]\n<<<END_RECORDING_BOUNDARIES>>>\n"
            ),
        )
        assert "Recording boundaries" not in (await store.get_note(job.note_id)).body


async def test_a_trimmed_recording_shows_its_boundaries_near_the_top(store):
    async with Service(reply=CUSTOMER) as service:
        _, job, _ = await customer_recording(
            store,
            service,
            lambda evidence: (
                "# " + evidence["resolved_title"] + "\n\n## Top line\nDone.\n\n## Notes\nBody.\n"
                '\n<<<RECORDING_BOUNDARIES>>>\n[{"start_seconds": 30, "end_seconds": 40, '
                '"kind": "unrelated_tail", "evidence": "Chat after the meeting.", '
                '"confidence": "high"}]\n<<<END_RECORDING_BOUNDARIES>>>\n'
            ),
        )
        body = (await store.get_note(job.note_id)).body
        assert (
            body.index("## Top line")
            < body.index("## Recording boundaries")
            < body.index("## Notes")
        )
        assert body.count("## Recording boundaries") == 1


async def test_a_default_prompt_follows_upgrades_and_an_edited_one_is_kept(store):
    from coppermind.recording_writeup import (
        DEFAULT_PROMPT,
        PREVIOUS_DEFAULT_PROMPTS,
        effective_prompt,
    )

    async with Service() as service:
        context = await setup_transcription(store, service)
        features = store.admin.features
        # Saved by an older build that stored the default it showed.
        config = context.config()
        config.assignments["writeup"].writeup_prompt = PREVIOUS_DEFAULT_PROMPTS[0]
        context.save_config(config)
        assert (await features.writeup_settings())["prompt"] == DEFAULT_PROMPT
        config.assignments["writeup"].writeup_prompt = PREVIOUS_DEFAULT_PROMPTS[0] + "\nMine."
        context.save_config(config)
        assert effective_prompt(context.config().assignments["writeup"].writeup_prompt).endswith(
            "Mine."
        )
        # Saving the page with the default untouched keeps following the default.
        await features.writeup_save({"writeup_prompt": DEFAULT_PROMPT})
        assert context.config().assignments["writeup"].writeup_prompt == ""
        await features.writeup_save({"writeup_glossary": "a = B"})
        assert (await features.writeup_settings())["glossary"] == "a = B"
