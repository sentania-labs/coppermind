"""Real store writes, archived calendars, provider calls and human approvals."""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from coppermind_store import context_service
from coppermind_store.context_service import ContextService
from coppermind_store.model_provider import ModelProvider, ProviderError
from coppermind_store.reconciler import reconcile_once

from coppermind.context_config import FeatureAssignment
from coppermind.store_protocol import (
    ArtifactQuery,
    CreateNote,
    NoteQuery,
    PatchFrontmatter,
    RenameNote,
)
from services.store.tests.test_calendar_import import event, ics


async def configure(store):
    context = store.context
    saved_connection = await context.save_connection(
        {
            "revision": 0,
            "connection": {
                "name": "Local",
                "provider": "compatible",
                "base_url": "http://local.example/v1",
            },
        }
    )

    def serve(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
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
    await context.discover({"connection": saved_connection["saved"]})
    await context.test_model({"connection": saved_connection["saved"], "model": "test-model"})
    await context.save_assignment(
        {
            "revision": 1,
            "assignment": FeatureAssignment(
                connection=saved_connection["saved"],
                model="test-model",
                enabled=True,
                fields=["account"],
            ).model_dump(),
        }
    )
    imported = await context.upload(
        {
            "calendar_id": "calendar",
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


async def test_dropbox_requires_stability_and_legacy_folders_are_ordinary(store):
    context = await configure(store)
    folder = store.notes_root / "Dropbox"
    folder.mkdir(parents=True)
    (folder / "export.ics").write_bytes(ics(event("second")))
    legacy = store.notes_root / "_Calendar/Work"
    legacy.mkdir(parents=True)
    (legacy / "ordinary.md").write_text("# Ordinary note")
    await context.dropbox.scan()
    assert len(context.archive.receipts()) == 1
    context.dropbox.seen = {
        key: (value[0], value[1] - 31) for key, value in context.dropbox.seen.items()
    }
    await context.dropbox.scan()
    await context.dropbox.process()
    assert len(context.archive.receipts()) == 2
    await reconcile_once(store)
    assert len((await store.list_notes(NoteQuery())).items) == 1


async def test_a_kind_sidecar_added_after_the_file_was_parked_rescues_it(store):
    context = await configure(store)
    folder = store.notes_root / "Dropbox"
    folder.mkdir(parents=True)
    (folder / "design.bin").write_bytes(b"not a calendar, no marker")

    async def settle():
        await context.dropbox.scan()
        context.dropbox.seen = {
            key: (value[0], value[1] - 31) for key, value in context.dropbox.seen.items()
        }
        await context.dropbox.scan()
        await context.dropbox.process()

    await settle()
    parked = [item for item in context.dropbox.snapshot()["items"] if item["id"]]
    assert [item["state"] for item in parked] == ["unsupported"]

    (folder / "design.bin.kind").write_text("artifact\n")
    await settle()

    states = [item["state"] for item in context.dropbox.snapshot()["items"] if item["id"]]
    assert states == ["succeeded"]
    stored = await store.list_artifacts(ArtifactQuery())
    assert [item.filename for item in stored.items] == ["design.bin"]


def receipts(context):
    return [item for item in context.dropbox.snapshot()["items"] if item["id"]]


async def settle(context):
    await context.dropbox.scan()
    context.dropbox.seen = {
        key: (value[0], value[1] - 31) for key, value in context.dropbox.seen.items()
    }
    await context.dropbox.scan()
    await context.dropbox.process()
    await context.dropbox.process()


async def test_a_dismissed_parked_drop_stays_dismissed_while_the_file_is_unchanged(store):
    context = await configure(store)
    folder = store.notes_root / "Dropbox"
    (folder / "other").mkdir(parents=True)
    (folder / "design.bin").write_bytes(b"not a calendar, no marker")
    (folder / "other" / "design.bin").write_bytes(b"a different file, no marker")
    await settle(context)
    assert sorted(item["state"] for item in receipts(context)) == ["unsupported", "unsupported"]
    parked = next(
        item
        for item in receipts(context)
        if context.dropbox.original(item["id"]) == b"not a calendar, no marker"
    )
    await context.dropbox.dismiss({"id": parked["id"]})

    for _ in range(3):
        await settle(context)
    remaining = receipts(context)
    assert parked["id"] not in {item["id"] for item in remaining}
    assert [item["state"] for item in remaining] == ["unsupported"]


async def test_a_dismissed_parked_drop_returns_when_a_sidecar_is_added(store):
    context = await configure(store)
    folder = store.notes_root / "Dropbox"
    folder.mkdir(parents=True)
    (folder / "design.bin").write_bytes(b"not a calendar, no marker")
    await settle(context)
    (parked,) = receipts(context)
    await context.dropbox.dismiss({"id": parked["id"]})
    await settle(context)
    assert receipts(context) == []

    (folder / "design.bin.kind").write_text("artifact\n")
    await settle(context)

    assert [item["state"] for item in receipts(context)] == ["succeeded"]
    assert [i.filename for i in (await store.list_artifacts(ArtifactQuery())).items] == [
        "design.bin"
    ]


async def test_a_dismissed_parked_drop_returns_when_the_file_changes(store):
    context = await configure(store)
    folder = store.notes_root / "Dropbox"
    folder.mkdir(parents=True)
    (folder / "design.bin").write_bytes(b"not a calendar, no marker")
    await settle(context)
    (parked,) = receipts(context)
    await context.dropbox.dismiss({"id": parked["id"]})
    await settle(context)
    assert receipts(context) == []

    (folder / "design.bin").write_bytes(b"not a calendar, no marker, edited")
    await settle(context)

    (again,) = receipts(context)
    assert again["state"] == "unsupported" and again["id"] != parked["id"]


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
    assert changed.frontmatter["account"] == ["Example"]
    assert changed.body == note.body
    restarted = ContextService(store)
    await restarted.resume_approval({"id": job.id})
    assert restarted.load_job(job.id).state == "applied"
    assert (await store.get_note(note.id)).content_hash == changed.content_hash


async def test_account_suggestion_joins_a_human_entered_list(store):
    from coppermind_store.enrichment_models import Suggestions

    context = await configure(store)
    note = await store.create_note(
        CreateNote(
            title="Old",
            body="Casey attended.",
            frontmatter={"date": "2026-10-14", "account": ["Xcel", "United"]},
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
                },
                {
                    "kind": "field",
                    "field": "account",
                    "value": ["United", "Acme"],
                    "reason": "User reviewed",
                    "quote": "Casey attended.",
                },
            ]
        }
    )
    job.state = "suggested"
    context.save_job(job)
    await context.approve({"id": job.id, "index": 0, "confirmation": "Apply selected suggestion"})
    assert (await store.get_note(note.id)).frontmatter["account"] == ["Xcel", "United", "Example"]
    await context.approve({"id": job.id, "index": 1, "confirmation": "Apply selected suggestion"})
    assert (await store.get_note(note.id)).frontmatter["account"] == [
        "Xcel",
        "United",
        "Example",
        "Acme",
    ]


async def test_date_approval_replaces_a_legacy_time_instead_of_being_overridden(store):
    from coppermind_store.enrichment_models import Suggestions

    from coppermind.schema import note_date

    context = await configure(store)
    config = context.config()
    config.assignments["enrichment"].fields.append("date")
    context.save_config(config)
    note = await store.create_note(
        CreateNote(
            title="Old",
            body="Casey attended.",
            frontmatter={
                "date": "2026-10-14",
                "time": "2026-10-14T10:00:00",
                "timezone": "America/New_York",
            },
        )
    )
    job = context.load_job(await context.queue_note(note.id))
    job.evidence = await context.evidence(job)
    job.result = Suggestions.model_validate(
        {
            "suggestions": [
                {
                    "kind": "field",
                    "field": "date",
                    "value": "2026-10-15T09:00:00",
                    "reason": "Date correction approved by user",
                    "quote": "Casey attended.",
                }
            ]
        }
    )
    job.state = "suggested"
    context.save_job(job)
    await context.approve({"id": job.id, "index": 0, "confirmation": "Apply selected suggestion"})
    changed = await store.get_note(note.id)
    assert changed.frontmatter["date"] == "2026-10-15T09:00:00"
    assert "time" not in changed.frontmatter
    assert "timezone" not in changed.frontmatter
    assert str(note_date(changed.frontmatter, store.control.schema())) == "2026-10-15"


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
            "calendar_id": "calendar",
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


@pytest.mark.parametrize("kind", ["contact", "attendee", "invitee"])
async def test_association_approvals_preserve_human_and_prior_values(store, kind):
    from coppermind_store.enrichment_models import Suggestions

    context = await configure(store)
    state = store.control.store.read("schema")
    body = dict(state.body)
    body["keys"] = [*body["keys"], {"name": "people", "kind": "list"}]
    store.control.store.write("schema", body, if_revision=state.revision)
    assignment = context.config().assignments["enrichment"].model_dump()
    mapping = {
        "contact": "contacts_field",
        "attendee": "attendees_field",
        "invitee": "invitees_field",
    }
    assignment[mapping[kind]] = "people"
    await context.save_assignment({"revision": context.config().revision, "assignment": assignment})
    imported = await context.upload(
        {
            "calendar_id": "calendar",
            "filename": "updated.ics",
            "content_base64": base64.b64encode(
                ics(event(sequence=1) + "\r\nATTENDEE;CN=Jordan:mailto:Jordan@example.com")
            ).decode(),
        }
    )
    await context.retry_import({"id": imported["id"]})
    note = await store.create_note(
        CreateNote(
            title="Planning",
            body="Casey and Jordan attended.",
            frontmatter={"date": "2026-10-14", "people": ["human@example.test"]},
        )
    )
    job = context.load_job(await context.queue_note(note.id))
    job.evidence = await context.evidence(job)
    candidate = job.evidence["events"][0]
    people = candidate["people"]
    assert len(people) == 2
    job.result = Suggestions.model_validate(
        {
            "suggestions": [
                {
                    "kind": kind,
                    "reason": "Explicitly confirmed",
                    "quote": "Casey and Jordan attended.",
                    "event_ids": [candidate["event_id"]],
                    "contact_ids": [person["contact_id"]],
                }
                for person in people
            ]
        }
    )
    job.state = "suggested"
    context.save_job(job)
    for index in range(2):
        await context.approve(
            {"id": job.id, "index": index, "confirmation": "Apply selected suggestion"}
        )
    final = await store.get_note(note.id)
    expected = [
        p["contact_id"] if kind == "contact" else p["address"].partition(":")[2] for p in people
    ]
    assert final.frontmatter["people"] == ["human@example.test", *expected]
    assert final.body == note.body
    assert final.frontmatter["reviewed"] is False
    ledger_key = {"contact": "contacts", "attendee": "attendees", "invitee": "invitees"}[kind]
    assert set(context.read("associations", {})[note.id][ledger_key]) == {
        p["contact_id"] for p in people
    }


async def test_running_job_at_limit_never_resends_after_restart(store):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Planning", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    job = context.load_job(await context.queue_note(note.id))
    job.state, job.attempts = "running", 2
    context.save_job(job)
    calls = []

    async def fail_if_called(*args):
        calls.append(args)
        raise RuntimeError("must not send")

    for _ in range(3):
        restarted = ContextService(store)
        restarted.provider.suggest = fail_if_called
        await restarted.tick()
    final = context.load_job(job.id)
    assert final.state == "failed"
    assert final.attempts == 2
    assert calls == []
    assert "interruption" in final.error


async def test_unexpected_provider_error_consumes_bounded_attempts(store):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Planning", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    job_id = await context.queue_note(note.id)
    calls = []

    async def malformed_response(*args):
        calls.append(1)
        raise KeyError("sensitive provider payload")

    context.provider.suggest = malformed_response
    for _ in range(4):
        await context.tick()
    final = context.load_job(job_id)
    assert final.state == "failed"
    assert final.attempts == 2
    assert len(calls) == 2
    assert "sensitive" not in final.error
    await context.run_job(final)
    assert len(calls) == 2


async def test_event_approval_filling_time_keeps_remaining_suggestions(store):
    from coppermind_store.enrichment_models import Suggestions

    context = await configure(store)
    await context.setup_lifecycle({"revision": context.config().revision})
    config = context.config()
    config.assignments["enrichment"].event_field = "account"
    context.save_config(config)
    note = await store.create_note(
        CreateNote(title="Planning", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    assert "time" not in note.frontmatter
    job = context.load_job(await context.queue_note(note.id))
    job.evidence = await context.evidence(job)
    candidate = job.evidence["events"][0]
    job.result = Suggestions.model_validate(
        {
            "suggestions": [
                {
                    "kind": "event",
                    "reason": "Calendar event",
                    "event_ids": [candidate["event_id"]],
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
    updated = await store.get_note(note.id)
    assert str(updated.frontmatter["date"]).startswith("2026-10-14T")
    assert "time" not in updated.frontmatter
    assert context.load_job(job.id).state == "suggested"
    assert context.load_job(job.id).error == ""


async def test_all_day_event_approval_does_not_shift_the_note_date(store):
    from coppermind_store.enrichment_models import Suggestions

    context = await configure(store)
    await context.setup_lifecycle({"revision": context.config().revision})
    config = context.config()
    config.assignments["enrichment"].event_field = "account"
    context.save_config(config)
    all_day = (
        "UID:holiday-1\r\nSEQUENCE:0\r\nSUMMARY:Offsite\r\n"
        "DTSTART;VALUE=DATE:20261014\r\nDTEND;VALUE=DATE:20261015"
    )
    imported = await context.upload(
        {
            "calendar_id": "calendar",
            "filename": "all-day.ics",
            "content_base64": base64.b64encode(ics(all_day)).decode(),
        }
    )
    await context.retry_import({"id": imported["id"]})
    note = await store.create_note(
        CreateNote(title="Planning", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    job = context.load_job(await context.queue_note(note.id))
    job.evidence = await context.evidence(job)
    candidate = next(item for item in job.evidence["events"] if item["start"] == "2026-10-14")
    job.result = Suggestions.model_validate(
        {
            "suggestions": [
                {
                    "kind": "event",
                    "reason": "Calendar event",
                    "event_ids": [candidate["event_id"]],
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
    updated = await store.get_note(note.id)
    assert "time" not in updated.frontmatter
    assert str(updated.frontmatter["date"]) == "2026-10-14"
    assert context.load_job(job.id).state == "suggested"


async def test_event_approval_filling_time_ignores_a_legacy_note_timezone(store):
    from coppermind_store.enrichment_models import Suggestions

    context = await configure(store)
    await context.setup_lifecycle({"revision": context.config().revision})
    config = context.config()
    config.assignments["enrichment"].event_field = "account"
    context.save_config(config)
    note = await store.create_note(
        CreateNote(
            title="Planning",
            body="Casey attended",
            frontmatter={"date": "2026-10-14", "timezone": "America/New_York"},
        )
    )
    assert "time" not in note.frontmatter
    job = context.load_job(await context.queue_note(note.id))
    job.evidence = await context.evidence(job)
    candidate = job.evidence["events"][0]
    job.result = Suggestions.model_validate(
        {
            "suggestions": [
                {
                    "kind": "event",
                    "reason": "Calendar event",
                    "event_ids": [candidate["event_id"]],
                },
            ]
        }
    )
    job.state = "suggested"
    context.save_job(job)
    await context.approve({"id": job.id, "index": 0, "confirmation": "Apply selected suggestion"})
    updated = await store.get_note(note.id)
    # The configured America/Chicago setting is the only timezone.
    assert str(updated.frontmatter["date"]) == "2026-10-14T09:00:00"
    assert "time" not in updated.frontmatter


def _answer(*suggestions):
    return {"suggestions": list(suggestions), "unresolved": []}


RULE_CASES = [
    (
        "unknown-record",
        lambda ev: {
            "kind": "event",
            "event_ids": ["not-supplied"],
            "reason": "r",
        },
        "pointed at calendar events or contacts it was not given",
    ),
    (
        "quote-not-found",
        lambda ev: {
            "kind": "filename",
            "value": "Title",
            "event_ids": [ev["events"][0]["event_id"]],
            "quote": "words nobody wrote",
            "reason": "r",
        },
        "quoted text that is not in the note or transcript",
    ),
    (
        "no-evidence",
        lambda ev: {"kind": "filename", "value": "Title", "reason": "r"},
        "no supporting quote or event",
    ),
    (
        "attendance-evidence",
        lambda ev: {
            "kind": "attendee",
            "quote": "Casey attended",
            "reason": "r",
        },
        "lacked a note quote or a known contact",
    ),
    (
        "event-missing",
        lambda ev: {"kind": "event", "quote": "Casey attended", "reason": "r"},
        "did not name a supplied event",
    ),
    (
        "contact-missing",
        lambda ev: {"kind": "contact", "quote": "Casey attended", "reason": "r"},
        "did not name a supplied contact",
    ),
    (
        "invalid-filename",
        lambda ev: {
            "kind": "filename",
            "value": "a/b",
            "event_ids": [ev["events"][0]["event_id"]],
            "reason": "r",
        },
        "single-line title without a path",
    ),
    (
        "invalid-filename",
        lambda ev: {
            "kind": "filename",
            "value": "   ",
            "event_ids": [ev["events"][0]["event_id"]],
            "reason": "r",
        },
        "blank or too long to use as a title",
    ),
    (
        "unmapped-field",
        lambda ev: {
            "kind": "field",
            "field": "secret_field",
            "value": "x",
            "quote": "Casey attended",
            "reason": "r",
        },
        "field secret_field, which enrichment is not allowed to fill",
    ),
    (
        "schema-mismatch",
        lambda ev: {
            "kind": "field",
            "field": "account",
            "value": {"not": "a list"},
            "quote": "Casey attended",
            "reason": "r",
        },
        "does not fit that field's type",
    ),
]


@pytest.mark.parametrize(("rule", "build", "text"), RULE_CASES, ids=[c[0] for c in RULE_CASES])
async def test_validation_failure_states_the_rule_and_logs_without_model_output(
    store, rule, build, text, caplog
):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Planning", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    job_id = await context.queue_note(note.id)

    async def answer(connection, key, assignment, system, evidence):
        return _answer(build(evidence) | {"reason": "SECRET-MODEL-TEXT"}), {}

    context.provider.suggest = answer
    with caplog.at_level("WARNING"):
        await context.run_job(context.load_job(job_id))
    final = context.load_job(job_id)
    assert final.state == "failed"
    assert final.error_rule == rule
    assert text in final.error
    assert f"({rule})" in final.error
    assert "Suggestion could not be validated" not in final.error
    assert final.error.rstrip().endswith(".")
    assert "SECRET-MODEL-TEXT" not in final.error
    logged = [r.getMessage() for r in caplog.records if job_id in r.getMessage()]
    assert logged
    assert all(rule in m and note.path in m and "SECRET-MODEL-TEXT" not in m for m in logged)


async def test_malformed_answer_names_the_problem_location_not_the_content(store, caplog):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Planning", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    job_id = await context.queue_note(note.id)

    async def answer(*args):
        return {"suggestions": [{"kind": "filename", "reason": ["SECRET-MODEL-TEXT"]}], "x": 1}, {}

    context.provider.suggest = answer
    with caplog.at_level("WARNING"):
        await context.run_job(context.load_job(job_id))
    final = context.load_job(job_id)
    assert final.error_rule == "answer-shape"
    assert "not in the expected format" in final.error
    assert "suggestions.0" in final.error
    assert "(unreadable name)" not in final.error
    assert "SECRET-MODEL-TEXT" not in final.error
    assert all("SECRET-MODEL-TEXT" not in r.getMessage() for r in caplog.records)


async def test_input_over_the_limit_records_size_and_limit(store):
    context = await configure(store)
    config = context.config()
    config.assignments["enrichment"].max_input_chars = 2000
    context.save_config(config)
    note = await store.create_note(
        CreateNote(title="Long", body="word " * 1000, frontmatter={"date": "2026-10-14"})
    )
    job_id = await context.queue_note(note.id)
    await context.run_job(context.load_job(job_id))
    final = context.load_job(job_id)
    assert final.state == "failed"
    assert final.error_rule == "input-limit"
    assert final.input_limit == 2000
    assert final.input_size > 2000
    assert f"{final.input_size:,} characters; the limit is 2,000" in final.error
    assert "Maximum note and transcript characters" in final.error


async def test_a_later_success_or_retry_clears_the_earlier_failure_rule(store):
    context = await configure(store)
    config = context.config()
    config.assignments["enrichment"].max_input_chars = 2000
    context.save_config(config)
    note = await store.create_note(
        CreateNote(title="Long", body="word " * 1000, frontmatter={"date": "2026-10-14"})
    )
    job_id = await context.queue_note(note.id)
    await context.run_job(context.load_job(job_id))
    assert context.load_job(job_id).error_rule == "input-limit"

    retry = await context.retry_job({"id": job_id})
    queued = context.load_job(retry["job"])
    assert (queued.state, queued.error, queued.error_rule) == ("queued", "", "")
    assert queued.input_size is None and queued.input_limit is None

    failed = context.load_job(retry["job"])
    failed.set_error("x", "input-limit", 5, 2)
    failed.attempts = failed.attempt_limit
    context.save_job(failed)
    await context.run_job(context.load_job(retry["job"]))
    exhausted = context.load_job(retry["job"])
    assert "Attempt limit reached" in exhausted.error
    assert exhausted.error_rule == "" and exhausted.input_limit is None


def _fake_transcript(store, monkeypatch, text, size_bytes=None):
    from types import SimpleNamespace as NS

    artifact = NS(
        name="t.txt",
        mime_type="text/plain",
        size_bytes=size_bytes if size_bytes is not None else len(text.encode()),
    )
    manifest = NS(current_revision=1, revisions=[NS(revision=1, artifacts=[artifact])])

    async def sources(note_id):
        return [NS(id="s1")]

    async def source(identity):
        return manifest

    async def document(*args):
        return NS(content=text)

    monkeypatch.setattr(store, "get_note_sources", sources)
    monkeypatch.setattr(store, "get_source", source)
    monkeypatch.setattr(store, "get_source_artifact", document)


async def _long_run(store, monkeypatch, text, limit=2000, **kwargs):
    context = await configure(store)
    config = context.config()
    config.assignments["enrichment"].max_input_chars = limit
    context.save_config(config)
    note = await store.create_note(
        CreateNote(title="Short", body="hi", frontmatter={"date": "2026-10-14"})
    )
    job_id = await context.queue_note(note.id)
    job = context.load_job(job_id)
    job.source_versions = {"s1": 1}
    context.save_job(job)
    _fake_transcript(store, monkeypatch, text, **kwargs)
    return context, job_id


async def test_non_ascii_transcript_under_the_limit_in_characters_is_not_rejected(
    store, monkeypatch
):
    context, job_id = await _long_run(store, monkeypatch, "", limit=3000)
    overhead = len(json.dumps(await context.evidence(context.load_job(job_id)), ensure_ascii=False))
    fits = 3000 - overhead - 100
    assert fits * 3 > 3000  # over the limit if bytes were counted as characters
    _fake_transcript(store, monkeypatch, "字" * fits)
    await context.run_job(context.load_job(job_id))
    assert context.load_job(job_id).error_rule != "input-limit"


async def test_non_ascii_size_is_reported_in_characters_the_limit_uses(store, monkeypatch):
    text = "字" * 2500  # 7,500 bytes, 2,500 characters
    context, job_id = await _long_run(store, monkeypatch, text)
    await context.run_job(context.load_job(job_id))
    final = context.load_job(job_id)
    assert final.error_rule == "input-limit"
    evidence_chars = len(json.dumps(await context.evidence(final), ensure_ascii=False))
    assert final.input_size == evidence_chars
    assert 2500 < final.input_size < 7500
    assert "at least" not in final.error


async def test_far_oversized_input_is_not_read_and_is_labelled_a_minimum(store, monkeypatch):
    context, job_id = await _long_run(store, monkeypatch, "x", size_bytes=100_000)
    await context.run_job(context.load_job(job_id))
    final = context.load_job(job_id)
    assert final.error_rule == "input-limit"
    assert final.input_size == 25_000
    assert "at least 25,000 characters" in final.error


async def test_a_model_no_longer_discovered_fails_the_job_with_a_named_rule(store, caplog):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Planning", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    job_id = await context.queue_note(note.id)
    context.write("discovery", {})

    with caplog.at_level("WARNING"):
        await context.run_job(context.load_job(job_id))

    final = context.load_job(job_id)
    assert final.state == "failed"
    assert final.error_rule == "model-not-available"
    assert "refresh models" in final.error
    assert "could not be validated" not in final.error
    assert any("model-not-available" in r.getMessage() for r in caplog.records)


async def test_calendar_and_dropbox_problem_counts_and_dismissal_agree(store):
    from services.store.tests.test_dropbox import calendar, finish

    dropbox = store.context.dropbox
    dropbox.submit("base.ics", calendar())
    await finish(store)
    failed = dropbox.submit("conflict.ics", calendar().replace(b"Planning", b"Changed"))
    unsupported = dropbox.submit("handout.pdf", b"%PDF-1.4 unsupported")
    await finish(store)
    await finish(store)
    problems = await store.get_problems()
    assert {p.kind for p in problems} == {"calendar_import", "dropbox"}
    assert (await store.get_status()).counters.active_problems == len(problems) == 2
    await dropbox.dismiss({"id": unsupported["id"]})
    assert (await store.get_status()).counters.active_problems == 1
    receipt_id = dropbox.records()["items"][failed["id"]]["parts"][0]["import_id"]
    await store.context.dismiss_import({"id": receipt_id})
    assert await store.get_problems() == []
    assert (await store.get_status()).counters.active_problems == 0


async def test_a_refused_answer_keeps_what_the_model_said_and_why(store, caplog):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Planning", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    job_id = await context.queue_note(note.id)

    async def answer(*args):
        return _answer({"kind": "field", "field": "nope", "reason": "r", "value": 1}), {}

    context.provider.suggest = answer
    with caplog.at_level("WARNING"):
        await context.run_job(context.load_job(job_id))
    final = context.load_job(job_id)
    assert final.state == "failed" and final.error_rule
    assert '"field": "nope"' in final.reply
    detail = await context.job_detail({"id": job_id})
    assert detail["reply"] == final.reply
    assert all("reply" not in job for job in (await context.snapshot())["jobs"])
    assert all('"nope"' not in r.getMessage() for r in caplog.records)


async def test_a_plain_value_error_is_named_not_reported_as_unvalidated(store):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Planning", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    job_id = await context.queue_note(note.id)

    async def answer(*args):
        raise ValueError("entity quote is not in the reviewed note")

    context.provider.suggest = answer
    await context.run_job(context.load_job(job_id))
    final = context.load_job(job_id)
    assert final.error_rule == "answer-rejected"
    assert "entity quote is not in the reviewed note" in final.error
    assert "could not be validated" not in final.error


async def test_a_timeout_backs_off_instead_of_retrying_at_once(store):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Planning", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    job_id = await context.queue_note(note.id)
    calls = []

    async def slow(*args):
        calls.append(1)
        raise ProviderError("quiet", "provider-no-response", waiting=True)

    context.provider.suggest = slow
    await context.run_job(context.load_job(job_id))
    waiting = context.load_job(job_id)
    assert waiting.state == "failed" and waiting.error_rule == "provider-no-response"
    assert waiting.retry_after > datetime.now(UTC).isoformat()
    assert waiting.attempts == 1

    # Ticks inside the wait leave the job alone, and do not send another request.
    await context.tick()
    await context.tick()
    assert len(calls) == 1 and context.load_job(job_id).state == "failed"

    # Once the wait is over, the next tick tries again.
    waiting.retry_after = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    context.save_job(waiting)
    await context.tick()
    assert len(calls) == 2

    # The last attempt promises no further automatic try, and an owner's retry still works.
    final = context.load_job(job_id)
    assert final.attempts == final.attempt_limit and final.retry_after == ""
    await context.retry_job({"id": job_id})
    assert context.load_job(job_id).retry_after == ""


async def test_an_error_that_recurs_on_the_same_model_does_not_back_off(store):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Planning", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    job_id = await context.queue_note(note.id)

    async def too_long(*args):
        raise ProviderError("still replying", "provider-too-long")

    context.provider.suggest = too_long
    await context.run_job(context.load_job(job_id))
    failed = context.load_job(job_id)
    assert failed.error_rule == "provider-too-long" and failed.retry_after == ""


async def test_the_test_call_is_realistic_and_records_speed(store):
    context = await configure(store)
    seen: dict[str, Any] = {}

    async def fake(connection, key, assignment, system, evidence):
        seen["size"] = len(json.dumps(evidence))
        seen["assignment"] = assignment
        return {"ok": True, "summary": "x"}, {
            "completion_tokens": 200,
            "streamed": True,
            "first_byte_ms": 2000,
            "elapsed_ms": 10000,
        }

    context.provider.suggest = fake
    connection = context.config().connections[0].id
    result = await context.test_model({"connection": connection, "model": "test-model"})
    assert result["speed"] == {"tokens_per_s": 25.0, "streamed": True}
    assert seen["size"] > 3000 and seen["assignment"].timeout_s == 120
    snapshot = await context.snapshot()
    assert snapshot["speed"][f"{connection}/test-model"]["tokens_per_s"] == 25.0


async def test_outcomes_count_every_state_and_the_last_success(store):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Planning", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    job_id = await context.queue_note(note.id)

    async def answer(*args):
        return _answer(), {}

    context.provider.suggest = answer
    await context.run_job(context.load_job(job_id))
    outcomes = (await context.snapshot())["outcomes"]
    assert outcomes["counts"] == {"suggested": 1} and outcomes["answered"] == 1
    assert outcomes["last_success"] == context.load_job(job_id).succeeded_at != ""


async def test_retry_history_and_automatic_recovery_after_limit_change(store):
    context = await configure(store)
    config = context.config()
    config.assignments["enrichment"].max_input_chars = 2000
    context.save_config(config)
    note = await store.create_note(
        CreateNote(title="Large", body="word " * 1000, frontmatter={"date": "2026-10-14"})
    )
    first = await context.queue_note(note.id)
    await context.run_job(context.load_job(first))
    retry = (await context.retry_job({"id": first}))["job"]
    assert retry != first
    assert context.load_job(first).state == "failed"
    await context.run_job(context.load_job(retry))
    assert [job["id"] for job in (await context.snapshot())["jobs"]] == [retry]
    assert (await context.job_detail({"id": retry}))["history"][0]["id"] == first
    config = context.config()
    config.assignments["enrichment"].max_input_chars = 10000
    context.save_config(config)
    await context.tick()
    jobs = sorted(context.jobs(), key=lambda job: job.created_at)
    assert len(jobs) == 3
    assert jobs[-1].config_hash == context.config_hash()


async def _two_failed_jobs(store):
    context = await configure(store)
    config = context.config()
    config.assignments["enrichment"].max_input_chars = 2000
    context.save_config(config)
    note = await store.create_note(
        CreateNote(title="Large", body="word " * 1000, frontmatter={"date": "2026-10-14"})
    )
    first = await context.queue_note(note.id)
    await context.run_job(context.load_job(first))
    second = (await context.retry_job({"id": first}))["job"]
    await context.run_job(context.load_job(second))
    assert {context.load_job(first).state, context.load_job(second).state} == {"failed"}
    return context, first, second


async def test_retry_all_failed_queues_one_job_per_note(store):
    context, first, second = await _two_failed_jobs(store)

    result = await context.retry_all_failed({})

    assert result == {"queued": 1, "errors": []}
    states = {job.id: job.state for job in context.jobs()}
    assert len(states) == 3
    assert states[first] == states[second] == "failed"
    assert sorted(states.values()) == ["failed", "failed", "queued"]


async def test_retry_job_rejects_a_job_that_is_not_the_newest_for_its_note(store):
    context, first, second = await _two_failed_jobs(store)
    older, newer = context.load_job(first), context.load_job(second)
    assert older.created_at <= newer.created_at
    jobs_before = len(context.jobs())

    with pytest.raises(ValueError, match="newest"):
        await context.retry_job({"id": older.id})

    assert len(context.jobs()) == jobs_before
    assert (await context.job_detail({"id": older.id}))["newest"] is False
    assert (await context.job_detail({"id": newer.id}))["newest"] is True


async def test_retry_all_failed_reports_notes_that_could_not_be_queued(store, monkeypatch):
    context, first, second = await _two_failed_jobs(store)

    async def refuse(*args, **kwargs):
        raise ValueError("model is not available")

    monkeypatch.setattr(context, "queue_note", refuse)

    result = await context.retry_all_failed({})

    assert result["queued"] == 0
    assert len(result["errors"]) == 1 and "model is not available" in result["errors"][0]
    assert len(context.jobs()) == 2


async def test_tick_recovers_a_model_not_available_failure_after_the_model_is_fixed(store):
    context = await configure(store)
    config = context.config()
    config.assignments["enrichment"].model = "undiscovered-model"
    context.save_config(config)
    note = await store.create_note(
        CreateNote(title="Planning", body="Notes", frontmatter={"date": "2026-10-14"})
    )
    first = await context.queue_note(note.id)
    await context.run_job(context.load_job(first))
    failed = context.load_job(first)
    assert (failed.state, failed.error_rule) == ("failed", "model-not-available")

    config = context.config()
    config.assignments["enrichment"].model = "test-model"
    context.save_config(config)
    await context.tick()

    jobs = sorted(context.jobs(), key=lambda job: job.created_at)
    assert [job.id for job in jobs][0] == first and len(jobs) == 2
    assert jobs[0].state == "failed"
    assert jobs[-1].state == "suggested"
    assert jobs[-1].config_hash == context.config_hash()


async def test_tick_recovers_a_model_not_available_failure_once_the_model_is_discovered(store):
    context = await configure(store)
    discovery = context.read("discovery", {})
    connection = context.config().assignments["enrichment"].connection
    identity = context.storage_identity(connection)
    context.write("discovery", {**discovery, identity: []})
    note = await store.create_note(
        CreateNote(title="Planning", body="Notes", frontmatter={"date": "2026-10-14"})
    )
    first = await context.queue_note(note.id)
    await context.run_job(context.load_job(first))
    assert context.load_job(first).error_rule == "model-not-available"

    await context.tick()
    assert len(context.jobs()) == 1

    context.write("discovery", {**discovery, identity: ["test-model"]})
    await context.tick()

    jobs = sorted(context.jobs(), key=lambda job: job.created_at)
    assert len(jobs) == 2
    assert jobs[0].id == first and jobs[0].state == "failed"
    assert jobs[-1].state == "suggested"


async def test_an_edited_failed_note_gets_no_new_job_until_it_settles(store, monkeypatch):
    context = await configure(store)
    config = context.config()
    config.assignments["enrichment"].max_input_chars = 2000
    context.save_config(config)
    note = await store.create_note(
        CreateNote(title="Large", body="word " * 1000, frontmatter={"date": "2026-10-14"})
    )
    first = await context.queue_note(note.id)
    await context.run_job(context.load_job(first))
    assert context.load_job(first).state == "failed"

    await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"account": "human correction"}), note.content_hash
    )
    await context.tick()
    await context.tick()
    assert [job.id for job in context.jobs()] == [first]

    monkeypatch.setattr(context_service, "NOTE_QUIET_PERIOD", timedelta(0))
    await context.tick()
    await context.tick()
    assert len(context.jobs()) == 2


async def test_a_calendar_change_does_not_requeue_failed_notes_when_lifecycle_is_off(store):
    context = await configure(store)
    config = context.config()
    config.assignments["enrichment"].max_input_chars = 2000
    context.save_config(config)
    assert not context.config().lifecycle_enabled
    failed = []
    for index in range(3):
        note = await store.create_note(
            CreateNote(
                title=f"Large {index}", body="word " * 1000, frontmatter={"date": "2026-10-14"}
            )
        )
        identity = await context.queue_note(note.id)
        await context.run_job(context.load_job(identity))
        assert context.load_job(identity).state == "failed"
        failed.append(identity)
    hash_before = context.evidence_hash()

    context.write("contact_corrections", {"someone": {"same_as": "other"}})
    assert context.evidence_hash() != hash_before
    await context.tick()
    await context.tick()

    assert sorted(job.id for job in context.jobs()) == sorted(failed)


async def test_a_renamed_failed_note_gets_a_new_job_at_its_new_path(store):
    context = await configure(store)
    config = context.config()
    config.assignments["enrichment"].max_input_chars = 2000
    context.save_config(config)
    note = await store.create_note(
        CreateNote(title="Large", body="word " * 1000, frontmatter={"date": "2026-10-14"})
    )
    first = await context.queue_note(note.id)
    await context.run_job(context.load_job(first))
    assert context.load_job(first).state == "failed"
    await context.tick()
    assert [job.id for job in context.jobs()] == [first]

    renamed = await store.rename_note(
        note.id, RenameNote(title="Renamed", preserve_body=True), note.content_hash
    )
    assert renamed.content_hash == note.content_hash and renamed.path != note.path
    await context.tick()

    jobs = sorted(context.jobs(), key=lambda job: job.created_at)
    assert len(jobs) == 2 and jobs[0].id == first
    assert jobs[-1].path == renamed.path


async def test_coppermind_own_writes_do_not_requeue_the_job(store, monkeypatch):
    note = await store.create_note(
        CreateNote(
            title="Old name",
            body="# Original heading\nCasey attended.",
            frontmatter={"date": "2026-10-14"},
        )
    )
    context = await configure(store)
    queued = await context.enqueue(
        {"note_ids": [note.id], "confirmation": "Send selected notes for enrichment"}
    )
    job = context.load_job(queued["jobs"][0])
    await context.run_job(job)
    assert job.state == "suggested", job.error
    await context.approve(
        {
            "id": job.id,
            "index": 0,
            "confirmation": "Apply selected suggestion",
            "accept_link_risk": True,
        }
    )
    applied = context.load_job(job.id)
    applied.state = "stale"
    context.save_job(applied)

    monkeypatch.setattr(context_service, "NOTE_QUIET_PERIOD", timedelta(0))
    await context.tick()
    await context.tick()
    assert [item.id for item in context.jobs()] == [job.id]

    written = await store.get_note(note.id)
    await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"account": "human correction"}), written.content_hash
    )
    await context.tick()
    assert len(context.jobs()) == 2


async def test_tick_recovers_when_transcript_revision_changes(store, monkeypatch):
    from types import SimpleNamespace

    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Planning", body="Notes", frontmatter={"date": "2026-10-14"})
    )
    first = await context.queue_note(note.id)
    failed = context.load_job(first)
    failed.state = "failed"
    failed.attempts = failed.attempt_limit
    failed.source_versions = {"source": 1}
    context.save_job(failed)

    async def sources(identity):
        return [SimpleNamespace(id="source")]

    async def source(identity):
        return SimpleNamespace(current_revision=2)

    async def leave_queued(job):
        pass

    async def source_text(identity):
        return []

    monkeypatch.setattr(context, "source_text", source_text)
    monkeypatch.setattr(store, "get_note_sources", sources)
    monkeypatch.setattr(store, "get_source", source)
    monkeypatch.setattr(context, "run_job", leave_queued)
    await context.tick()
    jobs = sorted(context.jobs(), key=lambda job: job.created_at)
    assert len(jobs) == 2
    assert jobs[0].id == first and jobs[0].state == "failed"
    assert jobs[1].source_versions == {"source": 2}
    assert jobs[1].state == "queued"
