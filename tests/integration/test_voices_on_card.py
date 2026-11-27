"""Who spoke, on a waiting note's card: each voice is named from a choice, straight through.

The card lists one row per voice with its talk time and a line it said. Choosing who a voice is
attaches it to that person as combining a voice with a person always did: other notes that used
the voice and recordings to come follow, the person joins the note's attendees, and nothing the
owner wrote in the note is touched.
"""

from __future__ import annotations

import json
import time

import pytest
from coppermind_store import filing, voice_card
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

EVENT = {
    "event_id": "evt-1",
    "title": "vSphere upgrade",
    "start": "2026-11-09T09:00:00-06:00",
    "end": "2026-11-09T10:00:00-06:00",
    "people": [
        {"name": "Dana Whitfield", "address": "mailto:dana@acme.test", "contact_id": "c1"},
        {"name": "Tom Reed", "address": "mailto:tom@acme.test", "contact_id": "c2"},
    ],
}


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
    # A tick does one piece of work; earlier recordings' clean-up can come first.
    for _ in range(5):
        await context.recordings.tick()
        job = context.recordings.load(accepted["id"])
        if job.state == "succeeded":
            break
    assert job.state == "succeeded", job.error
    return job, await store.get_note(job.note_id)


async def mark_reviewed(store, context, note_id):
    note = await store.get_note(note_id)
    await store.patch_frontmatter(
        note_id, PatchFrontmatter(set={"reviewed": True}), note.content_hash
    )
    stable = await store.get_note(note_id)
    context.lifecycle.observed[note_id] = (stable.content_hash, time.monotonic() - 31)
    await filing.file_reviewed(store)
    return await store.get_note(note_id)


async def lifecycle(store, service):
    context = await setup_transcription(store, service)
    await context.setup_lifecycle({"revision": context.config().revision})
    config = context.config()
    config.assignments["enrichment"].attendees_field = "attendees"
    context.save_config(config)
    return context


def rows(context, note_id):
    return voice_card.voices(context, context.review_notes.facts, note_id)


def row(context, note_id, label):
    """The voice the service called Speaker N, whatever it is called now. "Voice 2" is Speaker 2."""
    number = label.rsplit(" ", 1)[-1]
    return next(r for r in rows(context, note_id) if r["id"].endswith("|Speaker " + number))


async def choose(context, note_id, label, **choice):
    """What the card sends when the owner picks from a voice's dropdown."""
    voice = row(context, note_id, label)["id"]
    return await context.note_name_voice({"id": note_id, "voice": voice, "choice": choice})


def people(store):
    return {e.name: e for e in store.entities.load().entities.values() if e.kind == "person"}


async def transcript_text(store, job):
    source = await store.get_source(job.source_id)
    artifact = await store.get_source_artifact(
        job.source_id, source.current_revision, "transcript.txt"
    )
    return artifact.content


def with_invitation(context, monkeypatch, event=EVENT):
    async def candidates(note, descriptions=False):
        return [event]

    monkeypatch.setattr(context, "calendar_candidates", candidates)


async def test_a_card_lists_each_voice_with_its_time_a_line_it_said_and_a_plain_label(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        _, note = await record(store, context, "plaud/rec-1", AUDIO)
        page = await context.review_notes.listing({})
        card = next(n for n in page["notes"] if n["id"] == note.id)
        voices = card["voices"]
        assert [(v["label"], v["seconds"], v["person"], v["you"]) for v in voices] == [
            ("Voice 1", 79.0, None, False),
            ("Voice 2", 59.0, None, False),
        ]
        assert "backup vendor support matrix" in voices[1]["said"]
        assert voices[1]["said_at"] == "00:00:31"
        # The service's own label never reaches the owner, and nothing is written into the note.
        assert "Unknown voice" not in json.dumps(card)
        for gone in ("## Speakers", "Name this voice", "This is me", "Not identified yet"):
            assert gone not in note.body
        assert page["choices"]["you"] == ""


async def test_naming_a_voice_from_people_attaches_it_and_adds_the_attendee(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        reed = await store.entities.create(EntityInput(kind="person", name="Tom Reed"))
        job, note = await record(store, context, "plaud/rec-1", AUDIO)
        done = await choose(context, note.id, "Voice 2", person=reed["id"])
        named = next(v for v in done["voices"] if v["id"].endswith("|Speaker 2"))
        assert named["person"] == {"id": reed["id"], "name": "Tom Reed"}
        # Once a voice is a person, the person's name is what it is called.
        assert named["label"] == "Tom Reed"
        # Their voice is enrolled, the transcript says who spoke, the attendees say who was there.
        assert VoiceStore(context).summary(reed["id"])["signatures"]
        assert "Tom Reed: Hi Scott." in await transcript_text(store, job)
        after = await store.get_note(note.id)
        assert any("Tom Reed" in v for v in after.frontmatter["attendees"])
        assert all("@" not in v for v in after.frontmatter["attendees"])
        # Nothing was proposed and nothing asked for a second step.
        assert [
            s for s in (await context.lifecycle.detail({"note_id": note.id}))["suggestions"]
        ] == []
        assert not [v for v in VoiceStore(context).unknown_summaries().values() if "2" in v["name"]]
        assert len(people(store)) == 1


async def test_naming_a_voice_as_an_invitee_never_makes_a_second_person(store, monkeypatch):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        with_invitation(context, monkeypatch)
        _, note = await record(store, context, "plaud/rec-1", AUDIO)
        await context.review_notes.set_meeting({"id": note.id, "event_id": "evt-1"})
        page = await context.review_notes.listing({})
        card = next(n for n in page["notes"] if n["id"] == note.id)
        assert card["invitees"] == [
            {"name": "Dana Whitfield", "email": "dana@acme.test", "person_id": ""},
            {"name": "Tom Reed", "email": "tom@acme.test", "person_id": ""},
        ]
        await choose(
            context, note.id, "Voice 2", invitee={"name": "Tom Reed", "email": "tom@acme.test"}
        )
        found = people(store)
        assert set(found) == {"Tom Reed"} and found["Tom Reed"].emails == ["tom@acme.test"]
        # The invitation wrote him by name; now that he has a page, the attendee is a link.
        attendees = (await store.get_note(note.id)).frontmatter["attendees"]
        assert attendees.count(f"[[{found['Tom Reed'].path.removesuffix('.md')}]]") == 1
        assert "Tom Reed" not in attendees
        # Naming the same invitee again, or from another note, finds that person.
        _, second = await record(store, context, "plaud/rec-2", AUDIO + b"2")
        await context.review_notes.set_meeting({"id": second.id, "event_id": "evt-1"})
        card = next(
            n for n in (await context.review_notes.listing({}))["notes"] if n["id"] == second.id
        )
        assert next(i for i in card["invitees"] if i["name"] == "Tom Reed")["person_id"] == (
            found["Tom Reed"].id
        )
        await choose(
            context, second.id, "Voice 1", invitee={"name": "Tom Reed", "email": "tom@acme.test"}
        )
        assert set(people(store)) == {"Tom Reed"}


async def test_someone_typed_in_earlier_is_the_invitee_of_the_same_name_and_gets_the_address(
    store, monkeypatch
):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        with_invitation(context, monkeypatch)
        _, note = await record(store, context, "plaud/rec-1", AUDIO)
        await context.review_notes.set_meeting({"id": note.id, "event_id": "evt-1"})
        await choose(context, note.id, "Voice 2", new_name="Tom Reed")
        tom = people(store)["Tom Reed"]
        assert tom.emails == []
        card = next(
            n for n in (await context.review_notes.listing({}))["notes"] if n["id"] == note.id
        )
        # The card lists him once: the invitation's Tom Reed is the person already typed in.
        assert next(i for i in card["invitees"] if i["name"] == "Tom Reed")["person_id"] == tom.id
        await choose(
            context, note.id, "Voice 1", invitee={"name": "Tom Reed", "email": "tom@acme.test"}
        )
        assert set(people(store)) == {"Tom Reed"}
        assert people(store)["Tom Reed"].emails == ["tom@acme.test"]


async def test_an_invitee_who_is_already_known_by_email_is_that_person(store, monkeypatch):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        with_invitation(context, monkeypatch)
        made = await store.entities.create(
            EntityInput(kind="person", name="Thomas Reed", emails=["tom@acme.test"])
        )
        _, note = await record(store, context, "plaud/rec-1", AUDIO)
        await context.review_notes.set_meeting({"id": note.id, "event_id": "evt-1"})
        await choose(
            context, note.id, "Voice 2", invitee={"name": "Tom Reed", "email": "tom@acme.test"}
        )
        assert row(context, note.id, "Voice 2")["person"]["id"] == made["id"]
        assert set(people(store)) == {"Thomas Reed"}


async def test_only_someone_on_the_invitation_can_be_named_as_an_invitee(store, monkeypatch):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        with_invitation(context, monkeypatch)
        _, note = await record(store, context, "plaud/rec-1", AUDIO)
        await context.review_notes.set_meeting({"id": note.id, "event_id": "evt-1"})
        with pytest.raises(ValueError, match="not on the invitation"):
            await choose(
                context, note.id, "Voice 2", invitee={"name": "Eve", "email": "eve@evil.test"}
            )
        assert not people(store)


async def test_a_new_name_makes_one_person_and_the_same_name_again_finds_them(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        job, note = await record(store, context, "plaud/rec-1", AUDIO)
        await choose(context, note.id, "Voice 2", new_name="  Tom   Reed ")
        assert set(people(store)) == {"Tom Reed"}
        await choose(context, note.id, "Voice 1", new_name="tom reed")
        # Typing a name someone already has is that person, never a second one.
        assert set(people(store)) == {"Tom Reed"}
        assert [r["person"]["name"] for r in rows(context, note.id)] == ["Tom Reed", "Tom Reed"]
        assert "Unknown voice" not in await transcript_text(store, job)


async def test_two_people_with_one_name_are_not_guessed_between(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        await store.entities.create(EntityInput(kind="person", name="Sam Lee"))
        await store.entities.create(
            EntityInput(kind="person", name="Sam Lee", emails=["other@sam.test"])
        )
        _, note = await record(store, context, "plaud/rec-1", AUDIO)
        with pytest.raises(ValueError, match="More than one person is called Sam Lee"):
            await choose(context, note.id, "Voice 2", new_name="Sam Lee")
        with pytest.raises(ValueError, match="Type a name"):
            await choose(context, note.id, "Voice 2", new_name="  ")
        assert row(context, note.id, "Voice 2")["person"] is None


async def test_the_voice_is_carried_to_other_notes_and_to_the_next_recording(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        _, first = await record(store, context, "plaud/rec-1", AUDIO)
        second_job, second = await record(store, context, "plaud/rec-2", AUDIO + b"2")
        assert row(context, second.id, "Voice 2")["person"] is None
        await choose(context, first.id, "Voice 2", new_name="Tom Reed")
        # The other note that heard the same voice has it named, in its transcript and on its card.
        assert row(context, second.id, "Voice 2")["person"]["name"] == "Tom Reed"
        assert "Tom Reed: Hi Scott." in await transcript_text(store, second_job)
        # The voice nobody named is still asking.
        assert row(context, second.id, "Voice 1")["person"] is None
        # The next recording recognises it without being asked.
        third_job, third = await record(store, context, "plaud/rec-3", AUDIO + b"3")
        assert row(context, third.id, "Voice 2")["person"]["name"] == "Tom Reed"
        assert any("Tom Reed" in v for v in third.frontmatter["attendees"])
        assert "Unknown voice 2" not in await transcript_text(store, third_job)


async def test_the_owner_is_learned_once_and_offered_as_you_after_that(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        _, first = await record(store, context, "plaud/rec-1", AUDIO)
        with pytest.raises(ValueError, match="Say which voice is you"):
            await choose(context, first.id, "Voice 1", you=True)
        done = await choose(context, first.id, "Voice 1", new_name="Scott Bowe", me=True)
        owner = people(store)["Scott Bowe"]
        assert context.read("owner", {})["person_id"] == owner.id
        # The owner is listed first and called You.
        assert [
            (v["you"], v["person"]["name"] if v["person"] else None) for v in done["voices"]
        ] == [
            (True, "Scott Bowe"),
            (False, None),
        ]
        page = await context.review_notes.listing({})
        assert page["choices"]["you"] == owner.id
        # Asked again with another voice, the answer does not change who the owner is.
        await choose(context, first.id, "Voice 2", new_name="Tom Reed", me=True)
        assert context.read("owner", {})["person_id"] == owner.id
        # A later recording where the voice is recognised shows You, and "You" can be picked
        # for a voice that was not.
        _, later = await record(store, context, "plaud/rec-2", AUDIO + b"2")
        assert next(v for v in rows(context, later.id) if v["you"])["person"]["id"] == owner.id
        VoiceStore(context).set_recognition_enabled(False)
        _, third = await record(store, context, "plaud/rec-3", AUDIO + b"3")
        assert not any(v["you"] for v in rows(context, third.id))
        await choose(context, third.id, "Voice 1", you=True)
        assert next(v for v in rows(context, third.id) if v["you"])["person"]["id"] == owner.id


async def test_naming_leaves_everything_the_owner_wrote_in_the_note_alone(store, monkeypatch):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        with_invitation(context, monkeypatch)
        job, note = await record(store, context, "plaud/rec-1", AUDIO)
        await context.review_notes.set_meeting({"id": note.id, "event_id": "evt-1"})
        note = await store.get_note(note.id)
        mine = {"tags": ["mine"], "account": ["Contoso"], "type": "personal"}
        edited = note.body + "\n\n## My thoughts\nCall Dana about Unknown voice 2 before Friday.\n"
        await store.replace_note(
            note.id,
            ReplaceNote(
                frontmatter={**note.frontmatter, **mine, "attendees": ["Pat Kim"]}, body=edited
            ),
            note.content_hash,
        )
        before = await store.get_note(note.id)
        await choose(
            context, note.id, "Voice 2", invitee={"name": "Tom Reed", "email": "tom@acme.test"}
        )
        after = await store.get_note(note.id)
        assert after.body == before.body
        assert {k: after.frontmatter[k] for k in mine} == mine
        # What the owner listed stays, and the person who spoke joins them.
        assert (
            after.frontmatter["attendees"][0] == "Pat Kim"
            and len(after.frontmatter["attendees"]) == 2
        )
        assert after.frontmatter["reviewed"] is False
        # Nothing was proposed, and nothing the write-up made is marked out of date.
        assert not [j for j in context.jobs() if j.note_id == note.id and j.state != "rejected"]


async def test_a_generated_line_that_names_the_voice_follows_the_name_and_an_edited_one_does_not(
    store,
):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)

        async def write_text(connection, key, limits, system, evidence):
            return (
                "# " + evidence["resolved_title"] + "\n\n## Top line\nA call about vSphere.\n\n"
                "## Action items\n### Unknown voice 2\n- Check the support matrix.\n\n"
                "## Executive summary\nUnknown voice 2 said the upgrade is blocked.\n"
                "\n<<<RECORDING_BOUNDARIES>>>\n[]\n<<<END_RECORDING_BOUNDARIES>>>\n"
            ), {}

        context.provider.write_text = write_text
        _, note = await record(store, context, "plaud/rec-1", AUDIO)
        assert "Unknown voice 2 said the upgrade is blocked." in note.body
        mine = note.body.replace("Check the support matrix.", "Check the support matrix by Monday.")
        await store.replace_note(
            note.id, ReplaceNote(frontmatter=note.frontmatter, body=mine), note.content_hash
        )
        await choose(context, note.id, "Voice 2", new_name="Tom Reed")
        body = (await store.get_note(note.id)).body
        assert "Tom Reed said the upgrade is blocked." in body
        assert "by Monday." in body


async def test_changing_who_a_voice_is_moves_it_and_takes_the_wrong_person_off_the_note(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        job, note = await record(store, context, "plaud/rec-1", AUDIO)
        await choose(context, note.id, "Voice 2", new_name="Tom Reed")
        reed = people(store)["Tom Reed"]
        assert VoiceStore(context).summary(reed.id)["signatures"]
        await choose(context, note.id, "Voice 2", new_name="Ann Cole")
        found = people(store)
        assert row(context, note.id, "Voice 2")["person"]["name"] == "Ann Cole"
        transcript = await transcript_text(store, job)
        assert "Ann Cole: Hi Scott." in transcript and "Tom Reed" not in transcript
        # The voice left Tom, and went to Ann.
        assert not VoiceStore(context).summary(reed.id)["signatures"]
        assert VoiceStore(context).summary(found["Ann Cole"].id)["signatures"]
        attendees = (await store.get_note(note.id)).frontmatter["attendees"]
        assert any("Ann Cole" in v for v in attendees) and not any(
            "Tom Reed" in v for v in attendees
        )


async def test_a_voice_with_too_little_speech_is_named_in_this_recording_only(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        VoiceStore(context).set_recognition_enabled(False)
        job, note = await record(store, context, "plaud/rec-1", AUDIO)
        assert [r["label"] for r in rows(context, note.id)] == ["Voice 1", "Voice 2"]
        await choose(context, note.id, "Voice 2", new_name="Tom Reed")
        assert row(context, note.id, "Voice 2")["person"]["name"] == "Tom Reed"
        assert "Tom Reed: Hi Scott." in await transcript_text(store, job)
        assert any(
            "Tom Reed" in v for v in (await store.get_note(note.id)).frontmatter["attendees"]
        )
        # No voice was kept, so no later recording can be recognised from it.
        assert not VoiceStore(context).summary(people(store)["Tom Reed"].id)["signatures"]
        # It can still be changed.
        await choose(context, note.id, "Voice 2", new_name="Ann Cole")
        assert row(context, note.id, "Voice 2")["person"]["name"] == "Ann Cole"


async def test_a_note_named_on_its_card_is_reviewed_and_filed_as_usual(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        job, note = await record(store, context, "plaud/rec-1", AUDIO)
        await choose(context, note.id, "Voice 1", new_name="Scott Bowe", me=True)
        await choose(context, note.id, "Voice 2", new_name="Tom Reed")
        filed = await mark_reviewed(store, context, note.id)
        assert filing.filing_state(store, note.id)["filing_error"] is None
        assert filed.filed and not filed.path.startswith("Review/")
        assert "Tom Reed: Hi Scott." in await transcript_text(store, job)
        # Filing is settled: another pass has nothing left to do.
        assert await filing.file_reviewed(store) == {"filed": 0, "filing_failed": 0}


async def test_a_note_that_is_gone_or_a_voice_that_is_not_on_it_is_refused_in_a_sentence(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        _, note = await record(store, context, "plaud/rec-1", AUDIO)
        with pytest.raises(ValueError, match="no longer listed"):
            await context.note_name_voice({"id": "nope", "voice": "x|y", "choice": {"you": True}})
        with pytest.raises(ValueError, match="no longer on the recording"):
            await context.note_name_voice(
                {"id": note.id, "voice": "gone|Speaker 9", "choice": {"new_name": "Tom Reed"}}
            )
        with pytest.raises(ValueError, match="Choose who"):
            await context.note_name_voice(
                {"id": note.id, "voice": row(context, note.id, "Voice 1")["id"], "choice": "Tom"}
            )
        assert not people(store)


async def test_a_company_is_not_a_person_a_voice_can_be_named_as(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        company = await store.entities.create(EntityInput(kind="company", name="Acme"))
        _, note = await record(store, context, "plaud/rec-1", AUDIO)
        with pytest.raises(ValueError, match="That person is no longer listed"):
            await choose(context, note.id, "Voice 2", person=company["id"])
        assert row(context, note.id, "Voice 2")["person"] is None
        assert {e.name for e in store.entities.load().entities.values()} == {"Acme"}


async def test_writing_the_note_up_again_keeps_the_names_given_on_the_card(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        labels = []

        async def write_text(connection, key, limits, system, evidence):
            labels.append(evidence["allowed_speaker_labels"])
            speaker = next(
                name
                for name in evidence["allowed_speaker_labels"]
                if name == "Tom Reed" or name.endswith("voice 2")
            )
            return (
                "# " + evidence["resolved_title"] + f"\n\n## Top line\n{speaker} raised the "
                "support matrix.\n\n## Action items\nNo explicit commitments.\n\n"
                "## Executive summary\nHello.\n"
                "\n<<<RECORDING_BOUNDARIES>>>\n[]\n<<<END_RECORDING_BOUNDARIES>>>\n"
            ), {}

        context.provider.write_text = write_text
        job, note = await record(store, context, "plaud/rec-1", AUDIO)
        assert "Unknown voice 2 raised the support matrix." in note.body
        await choose(context, note.id, "Voice 2", new_name="Tom Reed")
        assert "Tom Reed raised the support matrix." in (await store.get_note(note.id)).body
        await context.review_notes.write_again(note.id, job.id)
        # The second write-up was given the name, not the service's label, and the name stays.
        assert "Tom Reed" in labels[1] and "Unknown voice 2" not in labels[1]
        again = await store.get_note(note.id)
        assert "Tom Reed raised the support matrix." in again.body
        assert row(context, note.id, "Voice 2")["person"]["name"] == "Tom Reed"
        assert any("Tom Reed" in v for v in again.frontmatter["attendees"])


async def test_an_invitee_whose_name_two_people_share_is_not_guessed_between(store, monkeypatch):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        with_invitation(context, monkeypatch)
        await store.entities.create(EntityInput(kind="person", name="Tom Reed"))
        await store.entities.create(EntityInput(kind="person", name="Tom Reed"))
        _, note = await record(store, context, "plaud/rec-1", AUDIO)
        await context.review_notes.set_meeting({"id": note.id, "event_id": "evt-1"})
        card = next(
            n for n in (await context.review_notes.listing({}))["notes"] if n["id"] == note.id
        )
        assert next(i for i in card["invitees"] if i["name"] == "Tom Reed")["person_id"] == ""
        with pytest.raises(ValueError, match="More than one person is called Tom Reed"):
            await choose(
                context, note.id, "Voice 2", invitee={"name": "Tom Reed", "email": "tom@acme.test"}
            )
        # Nobody was picked for him and no address was given to either of them.
        assert row(context, note.id, "Voice 2")["person"] is None
        assert all(p.emails == [] for p in people_all(store))


def people_all(store):
    return [e for e in store.entities.load().entities.values() if e.kind == "person"]


async def test_the_person_joins_the_attendees_of_every_waiting_note_that_heard_the_voice(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        _, first = await record(store, context, "plaud/rec-1", AUDIO)
        _, second = await record(store, context, "plaud/rec-2", AUDIO + b"2")
        await store.patch_frontmatter(
            second.id,
            PatchFrontmatter(set={"attendees": ["Pat Kim"]}),
            (await store.get_note(second.id)).content_hash,
        )
        await choose(context, first.id, "Voice 2", new_name="Tom Reed")
        tom = people(store)["Tom Reed"]
        link = f"[[{tom.path.removesuffix('.md')}]]"
        assert (await store.get_note(first.id)).frontmatter["attendees"] == [link]
        # The other note keeps what the owner listed, and now has him too.
        assert (await store.get_note(second.id)).frontmatter["attendees"] == ["Pat Kim", link]
        # A recording that comes after is recognised and has him once, not twice.
        _, third = await record(store, context, "plaud/rec-3", AUDIO + b"3")
        assert (await store.get_note(third.id)).frontmatter["attendees"] == [link]
