"""Notes arrive filled in: nothing is proposed, nothing goes stale, nothing the owner typed is
overwritten."""

from __future__ import annotations

import json
import uuid

from coppermind_store import filled_in
from coppermind_store.enrichment_models import EnrichmentJob, Suggestion, Suggestions

from coppermind.store_protocol import PatchFrontmatter
from services.store.tests.test_transcription_call import Service
from tests.integration.test_recordings import AUDIO
from tests.integration.test_speakers_named import (
    TOM,
    lifecycle,
    mark_reviewed,
    record,
    reply,
    type_into,
)


def answering(context, answers):
    """A writing service whose notes carry these answers for the note's properties."""
    block = (
        "\n<<<FIELD_SUGGESTIONS>>>\n"
        + json.dumps({"suggestions": answers})
        + "\n<<<END_FIELD_SUGGESTIONS>>>\n"
    )

    async def write_text(connection, key, limits, system, evidence):
        return (
            "# " + evidence["resolved_title"] + "\n\n## Top line\nA brief greeting.\n\n"
            "## Action items\nNo explicit commitments.\n\n## Executive summary\nHello.\n"
            + block
            + "\n<<<RECORDING_BOUNDARIES>>>\n[]\n<<<END_RECORDING_BOUNDARIES>>>\n"
        ), {}

    context.provider.write_text = write_text


ACME = [
    {"field": "account", "value": "Acme", "confidence": "high", "quote": "Acme"},
    {"field": "type", "value": "work", "confidence": "high", "quote": "x"},
    {"field": "tags", "value": ["vsphere", "A Brand New Tag"], "confidence": "medium"},
]


async def test_a_recording_arrives_with_its_properties_filled_in(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        answering(context, ACME)
        job, note = await record(store, context, "plaud/rec-1", AUDIO)
        # Work as well as personal, a new account, new tags, with nothing asked of the owner.
        assert note.frontmatter["type"] == "work"
        assert note.frontmatter["account"] == ["Acme"]
        assert note.frontmatter["tags"] == ["vsphere", "a-brand-new-tag"]
        assert "context" not in json.dumps(ACME)
        recorded = filled_in.recorded(context, note.id)
        assert recorded["account"] == ["Acme"] and recorded["type"] == "work"
        # No suggestion is left waiting for the owner to accept.
        detail = await context.lifecycle.detail({"note_id": note.id})
        assert "stale" not in detail
        assert [s for s in detail["suggestions"] if s["kind"] != "speaker"] == []


async def test_attendees_come_from_the_voices_that_were_recognised(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        config = context.config()
        config.assignments["enrichment"].attendees_field = "attendees"
        context.save_config(config)
        answering(context, [])
        _, first = await record(store, context, "plaud/rec-1", AUDIO)
        await type_into(store, first, {"Unknown voice 2": {"name": "Tom Reed"}})
        await mark_reviewed(store, context, first.id)
        _, second = await record(store, context, "plaud/rec-2", AUDIO + b"2")
        tom = next(e for e in store.entities.load().entities.values() if e.name == "Tom Reed")
        assert any(tom.path.removesuffix(".md") in v for v in second.frontmatter["attendees"])


async def test_what_the_owner_typed_survives_naming_a_voice_elsewhere_and_resummarize(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        answering(context, ACME)
        _, first = await record(store, context, "plaud/rec-1", AUDIO)
        second_job, second = await record(store, context, "plaud/rec-2", AUDIO + b"2")
        # The owner changes what was filled in.
        mine = {"type": "personal", "account": ["Contoso"], "tags": ["mine"]}
        await store.patch_frontmatter(second.id, PatchFrontmatter(set=mine), second.content_hash)
        # Naming a voice in the other note rewrites this one's body, as it always has.
        await type_into(store, first, {"Unknown voice 2": {"name": "Tom Reed"}})
        await mark_reviewed(store, context, first.id)
        after = await store.get_note(second.id)
        assert "- Tom Reed, 59 s. Named by you in another recording." in after.body
        assert {k: after.frontmatter[k] for k in mine} == mine
        detail = await context.lifecycle.detail({"note_id": second.id})
        assert "stale" not in detail
        assert [s for s in detail["suggestions"] if s["kind"] != "speaker"] == []
        # Writing the note up again, with different answers, leaves them alone too.
        answering(
            context,
            [
                {"field": "account", "value": "Fabrikam", "confidence": "high"},
                {"field": "type", "value": "work", "confidence": "high"},
                {"field": "tags", "value": ["other"], "confidence": "high"},
            ],
        )
        await context.review_notes.write_again(second.id, second_job.id)
        final = await store.get_note(second.id)
        assert {k: final.frontmatter[k] for k in mine} == mine


async def test_resummarize_refreshes_only_what_coppermind_filled(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        answering(context, ACME)
        job, note = await record(store, context, "plaud/rec-1", AUDIO)
        await store.patch_frontmatter(
            note.id, PatchFrontmatter(set={"tags": ["mine"]}), note.content_hash
        )
        answering(
            context,
            [
                {"field": "account", "value": "Fabrikam", "confidence": "high"},
                {"field": "tags", "value": ["other"], "confidence": "high"},
            ],
        )
        await context.review_notes.write_again(note.id, job.id)
        final = await store.get_note(note.id)
        assert final.frontmatter["account"] == ["Fabrikam"]
        assert final.frontmatter["tags"] == ["mine"]


async def test_upgrade_fills_what_was_waiting_once(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        answering(context, [])
        job, note = await record(store, context, "plaud/rec-1", AUDIO)
        assert note.frontmatter["type"] == "reference" and not note.frontmatter.get("account")
        # What an earlier version left waiting: the write-up's own answers, and a pending
        # suggestion from the second step for a property of the owner's own.
        key = "recording-writeups/" + job.id
        saved = context.read(key, {})
        saved["suggestions"] = [
            {"field": "account", "value": ["Acme"], "confidence": 0.9},
            {"field": "type", "value": "work", "confidence": 0.9},
            {"field": "tags", "value": ["vsphere"], "confidence": 0.9},
        ]
        context.write(key, saved)
        context.write("filled-in-upgrade", None)
        config = context.config()
        assert config.assignments["enrichment"].attendees_field == "attendees"
        pending = EnrichmentJob(
            id=uuid.uuid4().hex + uuid.uuid4().hex,
            note_id=note.id,
            note_hash=note.content_hash,
            evidence_hash="e",
            config_hash="c",
            schema_hash="s",
            path=note.path,
            created_at="2026-11-20T10:00:00+00:00",
            state="stale",
            result=Suggestions(
                suggestions=[
                    Suggestion(
                        kind="field",
                        field="tags",
                        value=["from-second-step"],
                        reason="said so",
                        quote="Hi Scott.",
                    )
                ]
            ),
        )
        context.save_job(pending)
        assert await filled_in.upgrade(context) == 1
        filled = await store.get_note(note.id)
        assert filled.frontmatter["type"] == "work"
        assert filled.frontmatter["account"] == ["Acme"]
        assert filled.frontmatter["tags"] == ["vsphere"]
        # Once: a value the owner then clears is not put back.
        await store.patch_frontmatter(
            note.id, PatchFrontmatter(set={"account": [], "tags": []}), filled.content_hash
        )
        assert await filled_in.upgrade(context) == 0
        assert not (await store.get_note(note.id)).frontmatter["account"]
        # And a note the owner had already filled is not touched at all.
        context.write("filled-in-upgrade", None)
        mine = await store.get_note(note.id)
        await store.patch_frontmatter(
            note.id, PatchFrontmatter(set={"type": "personal"}), mine.content_hash
        )
        await filled_in.upgrade(context)
        assert (await store.get_note(note.id)).frontmatter["type"] == "personal"


async def test_editing_a_note_leaves_nothing_stale_and_nothing_to_enrich(store):
    async with Service(reply=reply(TOM)) as service:
        context = await lifecycle(store, service)
        answering(context, ACME)
        _, note = await record(store, context, "plaud/rec-1", AUDIO)
        await store.patch_frontmatter(
            note.id, PatchFrontmatter(set={"tags": ["edited"]}), note.content_hash
        )
        detail = await context.lifecycle.detail({"note_id": note.id})
        assert "stale" not in detail and "speaker_attendees_notice" not in detail
        listed = await context.review_notes.listing({})
        row = next(n for n in listed["notes"] if n["id"] == note.id)
        assert "can_enrich" not in row and "suggestions" not in row
        assert "suggestions_on" not in listed
        # The one request that asked for suggestions again is gone.
        import coppermind_api.v1.workflow as workflow

        paths = [getattr(route, "path", "") for route in workflow.router.routes]
        assert not any(path.endswith("/suggestions") for path in paths)
