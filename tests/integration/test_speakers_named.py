"""Naming a voice from the recording note: type the name, mark the note reviewed, and the voice
is named in this note, in other notes that used it, and in the next recording."""

from __future__ import annotations

import time

from coppermind_store import filing
from coppermind_store.voice_store import VoiceStore

from coppermind.entities import EntityInput
from coppermind.store_protocol import PatchFrontmatter, ReplaceNote
from services.store.tests.test_transcription_call import REPLY, Service
from tests.integration.test_recordings import (
    AUDIO,
    request,
    setup_transcription,
    store_audio,
)

SCOTT = {"model": "synthetic", "version": "1", "vector": [1.0, 0.0, 0.0]}
TOM = {"model": "synthetic", "version": "1", "vector": [0.0, 1.0, 0.0]}
STRANGER = {"model": "synthetic", "version": "1", "vector": [0.0, 0.0, 1.0]}

SEGMENTS = [
    {
        "id": "s1",
        "start": 0.0,
        "end": 30.0,
        "speaker": "Speaker 1",
        "text": "Thanks for joining. We run vSphere 7 on the older cluster today.",
    },
    {
        "id": "s2",
        "start": 31.0,
        "end": 90.0,
        "speaker": "Speaker 2",
        "text": (
            "Hi Scott. Our upgrade to vSphere 8 is blocked by the backup vendor support matrix."
        ),
    },
    {
        "id": "s3",
        "start": 91.0,
        "end": 140.0,
        "speaker": "Speaker 1",
        "text": "Understood. I will send the compatibility guide by Friday.",
    },
]


def reply(second):
    return {
        **REPLY,
        "duration": 140.0,
        "segments": SEGMENTS,
        "speakers": [
            {"label": "Speaker 1", "speech_seconds": 80, "embeddings": [SCOTT]},
            {"label": "Speaker 2", "speech_seconds": 59, "embeddings": [second]},
        ],
    }


async def record(store, context, external_id, data):
    audio = await store_audio(store, name=external_id.replace("/", "-") + ".m4a", data=data)
    accepted = await context.accept_recording(request(audio, external_source_id=external_id))
    await context.recordings.tick()
    job = context.recordings.load(accepted["id"])
    assert job.state == "succeeded", job.error
    return job, await store.get_note(job.note_id)


async def type_into(store, note, typed):
    """What the owner does in Obsidian: type into the Speakers section."""
    body = note.body
    for label, text in typed.items():
        head, _, rest = body.partition(f"- {label},")
        rest = rest.replace("Name this voice:", "Name this voice: " + text.get("name", ""), 1)
        if text.get("me"):
            rest = rest.replace("[ ] This is me", "[x] This is me", 1)
        body = head + f"- {label}," + rest
    return await store.replace_note(
        note.id, ReplaceNote(frontmatter=note.frontmatter, body=body), note.content_hash
    )


async def mark_reviewed(store, context, note_id):
    note = await store.get_note(note_id)
    await store.patch_frontmatter(
        note_id, PatchFrontmatter(set={"reviewed": True}), note.content_hash
    )
    stable = await store.get_note(note_id)
    context.lifecycle.observed[note_id] = (stable.content_hash, time.monotonic() - 31)
    await filing.file_reviewed(store)
    return await store.get_note(note_id)


async def test_naming_the_unknown_voice_names_it_here_and_in_the_next_recording(store):
    async with Service(reply=reply(TOM)) as service:
        context = await setup_transcription(store, service)
        await context.setup_lifecycle({"revision": context.config().revision})
        config = context.config()
        config.assignments["enrichment"].attendees_field = "attendees"
        context.save_config(config)
        first_job, first = await record(store, context, "plaud/rec-1", AUDIO)
        assert "- Unknown voice 1, 1 min 19 s. Not identified yet." in first.body
        assert "- Unknown voice 2, 59 s. Not identified yet." in first.body
        # Two or three verbatim quotes, each pointing at the transcript, identify a voice.
        said = [line for line in first.body.splitlines() if line.lstrip().startswith("- Said:")]
        # One line per quote: two for the longer voice, one for the other.
        assert len(said) == 3 and "[[_Sources/" in said[2] and "|00:00:31]]" in said[2]
        assert '"Hi Scott.' not in said[2] and "backup vendor support matrix" in said[2]
        assert "<<coppermind" not in first.body

        typed = await type_into(
            store,
            first,
            {
                "Unknown voice 1": {"name": "Scott Bowe", "me": True},
                "Unknown voice 2": {"name": "Tom Reed"},
            },
        )
        assert "Name this voice: Tom Reed" in typed.body
        filed = await mark_reviewed(store, context, first.id)

        # Both voices are named in the note, the owner first and marked as such.
        speakers = filed.body.partition("## Top line")[0]
        assert "Scott Bowe]] (you), 1 min 19 s. Named by you." in speakers
        assert "(you), 1 min 19 s. Named by you. New person added to People." in speakers
        assert "Tom Reed" in speakers and "Name this voice" not in speakers
        assert speakers.index("Scott Bowe") < speakers.index("Tom Reed")
        people = {e.name: e for e in store.entities.load().entities.values()}
        assert {"Scott Bowe", "Tom Reed"} <= set(people)
        assert context.read("owner", {})["person_id"] == people["Scott Bowe"].id
        # The transcript carries the names, and the attendees were filled in.
        source = await store.get_source(first_job.source_id)
        transcript = await store.get_source_artifact(
            first_job.source_id, source.current_revision, "transcript.txt"
        )
        assert "Tom Reed: Hi Scott." in transcript.content
        assert "Unknown voice" not in transcript.content
        assert set(filed.frontmatter.get("attendees", [])) >= {"Scott Bowe", "Tom Reed"} or any(
            "Tom Reed" in str(v) for v in filed.frontmatter.get("attendees", [])
        )
        # Their voices are enrolled: nothing is left unknown.
        assert VoiceStore(context).unknown_summaries() == {}

        second_job, second = await record(store, context, "plaud/rec-2", AUDIO + b"2")
        speakers = second.body.partition("## Top line")[0]
        assert "Scott Bowe" in speakers and "(you)" in speakers
        assert "Tom Reed" in speakers and "Unknown voice" not in speakers
        assert "Identified by voice (strong match)." in speakers
        assert speakers.index("Scott Bowe") < speakers.index("Tom Reed")


async def lifecycle(store, service):
    context = await setup_transcription(store, service)
    await context.setup_lifecycle({"revision": context.config().revision})
    return context


async def test_a_first_name_alone_names_nobody_and_creates_nobody(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        reed = await store.entities.create(EntityInput(kind="person", name="Tom Reed"))
        await store.entities.create(EntityInput(kind="person", name="Tom Lee"))
        job, note = await record(store, context, "plaud/rec-1", AUDIO)
        await type_into(store, note, {"Unknown voice 2": {"name": "Tom"}})
        filed = await mark_reviewed(store, context, note.id)
        assert "Could not name this voice yet: Tom could be Tom Lee, Tom Reed" in filed.body
        assert "Write the full name or the link to that person to confirm." in filed.body
        assert "  - Name this voice: Tom" in filed.body
        assert "Named by you" not in filed.body
        assert "Unknown voice 2" in VoiceStore(context).unknown_for_note(note.id)[0]["name"] or (
            len(VoiceStore(context).unknown_for_note(note.id)) == 2
        )
        assert len(store.entities.load().entities) == 2
        # The owner types the full name and saves: that is all it takes.
        body = filed.body.replace("Name this voice: Tom", "Name this voice: Tom Reed")
        await store.replace_note(
            note.id, ReplaceNote(frontmatter=filed.frontmatter, body=body), filed.content_hash
        )
        again = await store.get_note(note.id)
        context.lifecycle.observed[note.id] = (again.content_hash, time.monotonic() - 31)
        await filing.file_reviewed(store)
        final = await store.get_note(note.id)
        assert "Could not name" not in final.body
        assert "|Tom Reed]], 59 s. Named by you." in final.body or "Tom Reed, 59 s" in final.body
        assert VoiceStore(context).summary(reed["id"])["signatures"]


async def test_a_first_name_with_one_match_in_people_is_not_taken_for_that_person(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        reed = await store.entities.create(EntityInput(kind="person", name="Tom Reed"))
        _, note = await record(store, context, "plaud/rec-1", AUDIO)
        await type_into(store, note, {"Unknown voice 2": {"name": "Tom"}})
        filed = await mark_reviewed(store, context, note.id)
        assert "Could not name this voice yet: Tom could be Tom Reed" in filed.body
        assert "  - Name this voice: Tom" in filed.body
        assert "Named by you" not in filed.body
        assert {e.name for e in store.entities.load().entities.values()} == {"Tom Reed"}
        assert not VoiceStore(context).summary(reed["id"])["signatures"]


async def test_the_same_voice_in_another_unedited_note_is_renamed_and_loses_its_questions(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        _, first = await record(store, context, "plaud/rec-1", AUDIO)
        second_job, second = await record(store, context, "plaud/rec-2", AUDIO + b"2")
        assert "- Unknown voice 2, 59 s. Not identified yet." in second.body
        await type_into(store, first, {"Unknown voice 2": {"name": "Tom Reed"}})
        await mark_reviewed(store, context, first.id)
        other = await store.get_note(second.id)
        assert "- Tom Reed, 59 s. Named by you in another recording." in other.body
        assert "backup vendor support matrix" not in other.body.partition("## Top line")[0]
        source = await store.get_source(second_job.source_id)
        transcript = await store.get_source_artifact(
            second_job.source_id, source.current_revision, "transcript.txt"
        )
        assert "Tom Reed: Hi Scott." in transcript.content
        # The voice nobody named is still asking.
        assert "- Unknown voice 1, 1 min 19 s. Not identified yet." in other.body


async def test_a_voice_with_too_little_speech_is_named_in_this_recording_only(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        VoiceStore(context).set_recognition_enabled(False)
        job, note = await record(store, context, "plaud/rec-1", AUDIO)
        assert "- Unknown voice 2 (recording-local), 59 s. Not identified yet." in note.body
        await type_into(store, note, {"Unknown voice 2 (recording-local)": {"name": "Tom Reed"}})
        filed = await mark_reviewed(store, context, note.id)
        assert "Tom Reed]], 59 s. Named by you. New person added to People. Too little speech" in (
            filed.body
        )
        source = await store.get_source(job.source_id)
        transcript = await store.get_source_artifact(
            job.source_id, source.current_revision, "transcript.txt"
        )
        assert "Tom Reed: Hi Scott." in transcript.content


async def test_this_is_me_without_a_name_asks_for_the_name_and_enrols_nobody(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        _, note = await record(store, context, "plaud/rec-1", AUDIO)
        await type_into(store, note, {"Unknown voice 1": {"name": "", "me": True}})
        filed = await mark_reviewed(store, context, note.id)
        assert 'Could not name this voice yet: Type your name after "Name this voice:"' in (
            filed.body
        )
        assert not context.read("owner", {})
        assert not store.entities.load().entities


async def test_a_voice_in_most_recordings_is_offered_as_the_owner_after_three(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        for number in (1, 2):
            _, note = await record(store, context, f"plaud/rec-{number}", AUDIO + bytes([number]))
            assert "Heard in" not in note.body
        _, third = await record(store, context, "plaud/rec-3", AUDIO + b"3")
        assert "  - Heard in 3 of your 3 recordings, so this may be you." in third.body


async def test_names_typed_in_the_note_are_applied_when_the_panel_finishes_review(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        _, note = await record(store, context, "plaud/rec-1", AUDIO)
        typed = await type_into(store, note, {"Unknown voice 2": {"name": "Tom Reed"}})
        detail = await context.lifecycle.detail({"note_id": note.id})
        assert detail["named_in_note"] == [{"label": "Unknown voice 2", "name": "Tom Reed"}]
        await context.lifecycle.complete(
            {
                "note_id": note.id,
                "if_match": typed.content_hash,
                "confirmation": "Complete review",
                "selected": [],
            }
        )
        done = await store.get_note(note.id)
        assert done.frontmatter["reviewed"] is True
        assert "Tom Reed]], 59 s. Named by you." in done.body
        assert {e.name for e in store.entities.load().entities.values()} == {"Tom Reed"}
