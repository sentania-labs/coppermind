"""Real store writes, archived calendars, provider calls and human approvals."""

import base64
import json

import httpx
import pytest
from coppermind_store.context_service import ContextService
from coppermind_store.model_provider import ModelProvider
from coppermind_store.reconciler import reconcile_once

from coppermind.context_config import FeatureAssignment
from coppermind.store_protocol import CreateNote, NoteQuery, PatchFrontmatter
from services.store.tests.test_calendar_import import event, ics


async def configure(store):
    context = store.context
    await context.save_source(
        {
            "revision": 0,
            "source": {
                "id": "work",
                "name": "Work",
                "folder": "_Calendar/Work",
            },
        }
    )
    await context.save_connection(
        {
            "revision": 1,
            "connection": {
                "id": "local",
                "name": "Local",
                "provider": "compatible",
                "base_url": "http://local.example/v1",
            },
        }
    )

    def serve(request):
        request_body = json.loads(request.content)
        content = '{"ok":true}'
        if "synthetic_connection_test" not in request_body["messages"][-1]["content"]:
            evidence = json.loads(request_body["messages"][-1]["content"])
            content = json.dumps(
                {
                    "suggestions": [
                        {
                            "kind": "filename",
                            "value": "Planning with Casey",
                            "reason": "The calendar event",
                            "event_ids": [evidence["events"][0]["event_id"]],
                        }
                    ],
                    "unresolved": [],
                }
            )
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        )

    context.provider = ModelProvider(httpx.MockTransport(serve))
    await context.test_model({"connection": "local", "model": "test-model"})
    await context.save_assignment(
        {
            "revision": 2,
            "assignment": FeatureAssignment(
                connection="local",
                model="test-model",
                enabled=True,
                fields=["account"],
            ).model_dump(),
        }
    )
    imported = await context.upload(
        {
            "calendar_id": "work",
            "filename": "work.ics",
            "content_base64": base64.b64encode(ics(event())).decode(),
        }
    )
    await context.retry_import({"id": imported["id"]})
    return context


async def test_calendar_to_suggestion_to_filename_preserves_body_and_review_state(store):
    note = await store.create_note(
        CreateNote(
            title="Old name",
            body="# Original heading\nCasey attended.",
            frontmatter={"date": "2026-10-14"},
        )
    )
    context = await configure(store)
    assert note.id in context.read("automatic_baseline", {})["ids"]
    await context.tick()
    assert not context.jobs()  # Existing corpus is never silently sent to a provider.
    queued = await context.enqueue(
        {"note_ids": [note.id], "confirmation": "Send selected notes for enrichment"}
    )
    job = context.load_job(queued["jobs"][0])
    await context.run_job(job)
    assert job.state == "suggested", job.error
    before = (await store.get_note(note.id)).body
    await context.approve(
        {
            "id": job.id,
            "index": 0,
            "confirmation": "Apply selected suggestion",
            "accept_link_risk": True,
        }
    )
    after = await store.get_note(note.id)
    assert after.body == before
    assert "Planning with Casey" in after.path
    assert after.frontmatter["reviewed"] is False
    assert after.id == note.id
    await reconcile_once(store)
    assert (await store.get_note(note.id)).body == before
    assert context.load_job(job.id).state == "applied"
    reconstructed = ContextService(store)
    assert reconstructed.archive.catalog().events
    assert (
        reconstructed.load_job(job.id).decisions[0]["confirmation"] == "Apply selected suggestion"
    )


async def test_stale_note_and_calendar_refuse_approval(store):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Old", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    identity = await context.queue_note(note.id)
    job = context.load_job(identity)
    await context.run_job(job)
    assert job.state == "suggested", job.error
    await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"account": "human correction"}), note.content_hash
    )
    with pytest.raises(ValueError, match="stale"):
        await context.approve(
            {
                "id": identity,
                "index": 0,
                "confirmation": "Apply selected suggestion",
                "accept_link_risk": True,
            }
        )
    assert (await store.get_note(note.id)).path == note.path


async def test_drop_folder_is_not_adopted_and_requires_stability(store):
    context = await configure(store)
    folder = store.notes_root / "_Calendar/Work"
    folder.mkdir(parents=True)
    (folder / "export.ics").write_bytes(ics(event("second")))
    (folder / "do-not-adopt.md").write_text("# Not a note")
    await context.watch()
    assert len(context.archive.receipts()) == 1
    context.seen = {key: (value[0], value[1] - 31) for key, value in context.seen.items()}
    await context.watch()
    assert len(context.archive.receipts()) == 2
    await reconcile_once(store)
    assert (await store.list_notes(NoteQuery())).items == []


async def test_calendar_invitation_cannot_become_attendance_without_note_evidence(store):
    from coppermind_store.enrichment_models import Suggestions

    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Old", body="Planning", frontmatter={"date": "2026-10-14"})
    )
    job = context.load_job(await context.queue_note(note.id))
    evidence = await context.evidence(job)
    candidate = evidence["events"][0]
    result = Suggestions.model_validate(
        {
            "suggestions": [
                {
                    "kind": "attendee",
                    "reason": "Accepted invitation",
                    "event_ids": [candidate["event_id"]],
                    "contact_ids": [candidate["people"][0]["contact_id"]],
                }
            ]
        }
    )
    with pytest.raises(ValueError, match="attendance"):
        context.validate_result(result, evidence)


async def test_field_approval_and_interrupted_receipt_resume(store, monkeypatch):
    from coppermind_store.enrichment_models import Suggestions

    context = await configure(store)
    note = await store.create_note(
        CreateNote(
            title="Old", body="# Heading\nCasey attended.", frontmatter={"date": "2026-10-14"}
        )
    )
    job = context.load_job(await context.queue_note(note.id))
    job.evidence = await context.evidence(job)
    job.result = Suggestions.model_validate(
        {
            "suggestions": [
                {
                    "kind": "field",
                    "field": "account",
                    "value": "Example",
                    "reason": "User reviewed",
                    "quote": "Casey attended.",
                }
            ]
        }
    )
    job.state = "suggested"
    context.save_job(job)
    original = context.save_job

    def fail_after_write(record):
        if record.state == "applied":
            raise OSError("simulated loss before final receipt")
        original(record)

    monkeypatch.setattr(context, "save_job", fail_after_write)
    with pytest.raises(OSError):
        await context.approve(
            {"id": job.id, "index": 0, "confirmation": "Apply selected suggestion"}
        )
    changed = await store.get_note(note.id)
    assert changed.frontmatter["account"] == "Example"
    assert changed.body == note.body
    restarted = ContextService(store)
    await restarted.resume_approval({"id": job.id})
    assert restarted.load_job(job.id).state == "applied"
    assert (await store.get_note(note.id)).content_hash == changed.content_hash


async def test_new_notes_queue_but_paused_enrichment_sends_nothing(store):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="New", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    config = context.config()
    config.assignments["enrichment"].paused = True
    context.save_config(config)
    await context.tick()
    assert context.jobs() == []
    config.assignments["enrichment"].paused = False
    context.save_config(config)
    await context.tick()
    assert context.jobs()[0].note_id == note.id
    assert context.jobs()[0].state == "suggested"


async def test_invalid_device_metadata_cannot_block_other_jobs(store):
    context = await configure(store)
    invalid = await store.create_note(CreateNote(title="Invalid", body="Body"))
    path = store.notes_root / invalid.path
    path.write_text(path.read_text().replace("reviewed: false", "reviewed: [wrong]"))
    await reconcile_once(store)
    valid = await store.create_note(
        CreateNote(title="Valid", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    await context.tick()
    assert invalid.id in context.eligibility_errors
    assert any(job.note_id == valid.id and job.state == "suggested" for job in context.jobs())


async def test_date_approval_requires_and_allows_fresh_matching(store):
    from coppermind_store.enrichment_models import Suggestions

    context = await configure(store)
    config = context.config()
    config.assignments["enrichment"].fields.append("date")
    context.save_config(config)
    note = await store.create_note(
        CreateNote(title="Old", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    job = context.load_job(await context.queue_note(note.id))
    job.evidence = await context.evidence(job)
    job.result = Suggestions.model_validate(
        {
            "suggestions": [
                {
                    "kind": "field",
                    "field": "date",
                    "value": "2026-10-15",
                    "quote": "Casey attended",
                    "reason": "Date correction approved by user",
                },
                {
                    "kind": "filename",
                    "value": "Better name",
                    "quote": "Casey attended",
                    "reason": "Subject",
                },
            ]
        }
    )
    job.state = "suggested"
    context.save_job(job)
    await context.approve({"id": job.id, "index": 0, "confirmation": "Apply selected suggestion"})
    assert context.load_job(job.id).state == "stale"
    retried = await context.retry_job({"id": job.id})
    assert retried["job"] != job.id
    assert context.load_job(retried["job"]).state == "queued"


async def test_noop_calendar_reexport_keeps_rejection_and_evidence_version(store):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Planning", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    identity = await context.queue_note(note.id)
    job = context.load_job(identity)
    await context.run_job(job)
    await context.reject({"id": identity})
    before = context.evidence_hash()
    exported = ics(event() + "\r\nDTSTAMP:20261014T160000Z")
    receipt = await context.upload(
        {
            "calendar_id": "work",
            "filename": "new.ics",
            "content_base64": base64.b64encode(exported).decode(),
        }
    )
    await context.retry_import({"id": receipt["id"]})
    assert context.evidence_hash() == before
    assert await context.queue_note(note.id) == identity
    assert context.load_job(identity).state == "rejected"


async def test_individual_rejection_leaves_other_proposal_available(store):
    from coppermind_store.enrichment_models import Suggestions

    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Old", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    job = context.load_job(await context.queue_note(note.id))
    job.evidence = await context.evidence(job)
    job.result = Suggestions.model_validate(
        {
            "suggestions": [
                {
                    "kind": "field",
                    "field": "account",
                    "value": "Example",
                    "quote": "Casey attended",
                    "reason": "Proposed account",
                },
                {
                    "kind": "filename",
                    "value": "Planning",
                    "quote": "Casey attended",
                    "reason": "Subject",
                },
            ]
        }
    )
    job.state = "suggested"
    context.save_job(job)
    await context.reject({"id": job.id, "index": 0})
    assert context.load_job(job.id).state == "suggested"
    await context.approve(
        {
            "id": job.id,
            "index": 1,
            "confirmation": "Apply selected suggestion",
            "accept_link_risk": True,
        }
    )
    final = await store.get_note(note.id)
    assert final.frontmatter.get("account") != "Example"
    assert context.load_job(job.id).state == "applied"


async def test_mistaken_contact_association_can_be_removed_with_audit(store):
    from coppermind_store.context_service import fingerprint
    from coppermind_store.enrichment_models import Suggestions

    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Planning", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    job = context.load_job(await context.queue_note(note.id))
    job.evidence = await context.evidence(job)
    candidate = job.evidence["events"][0]
    contact_id = candidate["people"][0]["contact_id"]
    job.result = Suggestions.model_validate(
        {
            "suggestions": [
                {
                    "kind": "contact",
                    "reason": "Calendar invitee",
                    "event_ids": [candidate["event_id"]],
                    "contact_ids": [contact_id],
                }
            ]
        }
    )
    job.state = "suggested"
    context.save_job(job)
    with pytest.raises(ValueError, match="identities"):
        await context.approve(
            {
                "id": job.id,
                "index": 0,
                "value": "Different person",
                "confirmation": "Apply selected suggestion",
            }
        )
    await context.approve({"id": job.id, "index": 0, "confirmation": "Apply selected suggestion"})
    current = context.read("associations", {})[note.id]
    assert contact_id in current["contacts"]
    correction = await context.correct_association(
        {
            "note_id": note.id,
            "kind": "contacts",
            "target": contact_id,
            "note_hash": note.content_hash,
            "associations_hash": fingerprint(current),
            "confirmation": "Remove mistaken association",
        }
    )
    assert contact_id not in context.read("associations", {})[note.id]["contacts"]
    receipt = context.read("association-corrections/" + correction["corrected"], {})
    assert receipt["state"] == "succeeded"
    assert receipt["confirmation"] == "Remove mistaken association"
    assert (await store.get_note(note.id)).body == note.body
