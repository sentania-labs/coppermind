"""Real filesystem and database lifecycle boundaries, with no external model calls."""

import pytest
from coppermind_store.filing import file_note

from coppermind.store_protocol import CreateNote, PatchFrontmatter


async def enable(store):
    await store.context.setup_lifecycle({"revision": 0})


async def test_review_requires_meeting_time_and_records_final_human_metadata(store):
    await enable(store)
    note = await store.create_note(
        CreateNote(
            title="Planning",
            body="Human prose.",
            frontmatter={"type": "work", "date": "2026-10-16"},
        )
    )
    with pytest.raises(ValueError, match="meeting time"):
        await store.context.complete_review(
            {"note_id": note.id, "if_match": note.content_hash, "confirmation": "Complete review"}
        )
    note = await store.patch_frontmatter(
        note.id,
        PatchFrontmatter(
            set={
                "time_unknown": True,
                "attendees": ["alex@example.com"],
                "tags": ["human-choice"],
            }
        ),
        if_match=note.content_hash,
    )
    payload = {"note_id": note.id, "if_match": note.content_hash, "confirmation": "Complete review"}
    first = await store.context.complete_review(payload)
    assert first["state"] == "complete"
    assert await store.context.complete_review(payload) == first
    await file_note(store, note.id)
    result = await store.get_note(note.id)
    assert result.body == note.body
    assert result.path.startswith("2026-10/")
    assert result.frontmatter["reviewed"] is True
    dossiers = store.context.read("dossiers", {})
    assert len(dossiers) == 1
    assert next(iter(dossiers.values()))["notes"][0]["properties"]["tags"] == ["human-choice"]


@pytest.mark.parametrize("kind", ["personal", "reference", "journal"])
async def test_nonwork_needs_no_time_and_stale_completion_does_not_overwrite(store, kind):
    await enable(store)
    note = await store.create_note(CreateNote(title="Other note", frontmatter={"type": kind}))
    old = note.content_hash
    note = await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"tags": ["changed"]}), if_match=old
    )
    with pytest.raises(ValueError, match="changed or has not synced"):
        await store.context.complete_review(
            {"note_id": note.id, "if_match": old, "confirmation": "Complete review"}
        )
    await store.context.complete_review(
        {"note_id": note.id, "if_match": note.content_hash, "confirmation": "Complete review"}
    )
    assert (await store.get_note(note.id)).frontmatter["tags"] == ["changed"]


async def test_direct_review_with_missing_meeting_time_blocks_filing(store):
    await enable(store)
    note = await store.create_note(CreateNote(title="Meeting", frontmatter={"type": "work"}))
    await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"reviewed": True}), if_match=note.content_hash
    )
    with pytest.raises(ValueError, match="meeting time"):
        await file_note(store, note.id)
    assert (await store.get_note(note.id)).path.startswith("Review/")


async def test_dropbox_unknown_identity_preserves_and_requires_choice(store):
    from services.store.tests.test_dropbox import calendar

    item = store.context.dropbox.submit("export.ics", calendar())
    await store.context.dropbox.process()
    assert store.context.dropbox.records()["items"][item["id"]]["state"] == "needs_identity"
    assert store.context.archive.receipts() == []
    await store.context.identify_drop({"id": item["id"], "part": 0, "name": "Family"})
    await store.context.dropbox.process()
    receipt = store.context.archive.receipts()[0]
    assert store.context.archive.process(receipt.id).state == "succeeded"
    await store.context.dropbox.process()
    assert store.context.dropbox.records()["items"][item["id"]]["state"] == "succeeded"
    assert (store.context.root / "dropbox-originals" / item["id"]).read_bytes() == calendar()


async def test_late_calendar_only_marks_affected_reviewed_notes_and_does_not_edit(store):
    from services.store.tests.test_dropbox import calendar

    await enable(store)
    affected = await store.create_note(
        CreateNote(title="Meeting day", frontmatter={"date": "2026-10-16"})
    )
    other = await store.create_note(
        CreateNote(title="Other day", frontmatter={"date": "2026-09-01"})
    )
    for note in [affected, other]:
        await store.context.complete_review(
            {"note_id": note.id, "if_match": note.content_hash, "confirmation": "Complete review"}
        )
    before = (await store.get_note(affected.id)).content_hash
    store.context.dropbox.submit("work.ics", calendar("work"))
    await store.context.dropbox.process()
    for receipt in store.context.archive.receipts():
        store.context.archive.process(receipt.id)
    await store.context.lifecycle.check_calendars()
    updates = store.context.read("calendar-followups", {})
    assert set(updates) == {affected.id}
    assert (await store.get_note(affected.id)).content_hash == before
    update = updates[affected.id]
    await store.context.resolve_calendar(
        {
            "note_id": affected.id,
            "if_match": before,
            "calendar_hash": update["calendar_hash"],
            "event_id": update["candidates"][0]["event_id"],
        }
    )
    await store.context.lifecycle.check_calendars()
    assert store.context.read("calendar-followups", {}) == {}
    assert (await store.get_note(affected.id)).content_hash == before


async def test_unreviewing_withdraws_dossier_contribution(store):
    await enable(store)
    note = await store.create_note(
        CreateNote(title="Note", frontmatter={"attendees": ["alex@example.com"]})
    )
    await store.context.complete_review(
        {"note_id": note.id, "if_match": note.content_hash, "confirmation": "Complete review"}
    )
    assert store.context.read("dossiers", {})
    current = await store.get_note(note.id)
    await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"reviewed": False}), if_match=current.content_hash
    )
    await store.context.lifecycle.check_calendars()
    assert store.context.read("dossiers", {}) == {}


async def test_one_initial_pass_does_not_rerun_for_ordinary_edits(store, monkeypatch):
    from tests.integration.test_context_enrichment import configure

    await configure(store)
    await store.context.setup_lifecycle({"revision": store.context.config().revision})
    note = await store.create_note(CreateNote(title="Planning", frontmatter={"date": "2026-10-14"}))
    calls = []

    async def run(job):
        calls.append(job.id)
        job.state = "suggested"
        store.context.save_job(job)

    monkeypatch.setattr(store.context, "run_job", run)
    await store.context.tick()
    assert len(calls) == 1
    note = await store.get_note(note.id)
    await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"tags": ["human-edit"]}), if_match=note.content_hash
    )
    await store.context.tick()
    assert len(calls) == 1


async def test_review_resume_after_flag_write_uses_exact_expected_hash(store, monkeypatch):
    await enable(store)
    note = await store.create_note(CreateNote(title="Recover review"))
    payload = {"note_id": note.id, "if_match": note.content_hash, "confirmation": "Complete review"}
    original = store.patch_frontmatter

    async def interrupt(*args, **kwargs):
        result = await original(*args, **kwargs)
        if result.frontmatter.get("reviewed"):
            raise OSError("simulated process interruption after durable review")
        return result

    monkeypatch.setattr(store, "patch_frontmatter", interrupt)
    with pytest.raises(OSError):
        await store.context.complete_review(payload)
    assert store.context.read("review-completions/" + note.id, {})["state"] == "applying"
    monkeypatch.setattr(store, "patch_frontmatter", original)
    assert (await store.context.complete_review(payload))["state"] == "complete"


async def test_reviewed_device_file_must_settle_before_filing(store, monkeypatch):
    import time

    from coppermind_store import note_lifecycle

    await enable(store)
    note = await store.create_note(CreateNote(title="From phone"))
    note = await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"reviewed": True}), if_match=note.content_hash
    )
    with pytest.raises(ValueError, match="remain stable"):
        await file_note(store, note.id)
    stable_after = time.monotonic() + 31
    monkeypatch.setattr(note_lifecycle.clock, "monotonic", lambda: stable_after)
    assert await file_note(store, note.id)


async def test_selected_suggestion_and_review_are_one_resumable_action(store):
    from tests.integration.test_context_enrichment import configure

    await configure(store)
    await store.context.setup_lifecycle({"revision": store.context.config().revision})
    note = await store.create_note(
        CreateNote(
            title="Original",
            body="Authored body stays byte-for-byte.",
            frontmatter={"date": "2026-10-14"},
        )
    )
    job_id = await store.context.queue_note(note.id)
    await store.context.run_job(store.context.load_job(job_id))
    assert store.context.load_job(job_id).state == "suggested"
    with pytest.raises(ValueError, match="confirm filename"):
        await store.context.complete_review(
            {
                "note_id": note.id,
                "if_match": note.content_hash,
                "job_id": job_id,
                "selected": [0],
                "confirmation": "Complete review",
            }
        )
    assert not store.context.read("review-completions/" + note.id, {})
    result = await store.context.complete_review(
        {
            "note_id": note.id,
            "if_match": note.content_hash,
            "job_id": job_id,
            "selected": [0],
            "accept_link_risk": True,
            "confirmation": "Complete review",
        }
    )
    assert result["state"] == "complete"
    final = await store.get_note(note.id)
    assert final.path.endswith("/Planning with Casey.md")
    assert final.title == note.title  # The authored heading is part of the preserved body.
    assert final.body == note.body
    assert final.frontmatter["reviewed"] is True


async def test_dropbox_binds_existing_calendar_before_import_and_remembers_uid(store):
    from coppermind.context_config import CalendarSource
    from services.store.tests.test_dropbox import calendar

    config = store.context.config()
    config.sources = [CalendarSource(id="work", name="Work", folder="Calendars/Work")]
    store.context.write("config", config.model_dump())
    data = calendar("physical-work")
    receipt = store.context.archive.submit("work", "old.ics", data)
    assert store.context.archive.process(receipt.id).state == "succeeded"
    item = store.context.dropbox.submit("new.ics", data)
    await store.context.dropbox.process()
    assert store.context.dropbox.records()["items"][item["id"]]["state"] == "needs_identity"
    await store.context.identify_drop({"id": item["id"], "part": 0, "calendar_id": "work"})
    await store.context.dropbox.process()
    assert {r.calendar_id for r in store.context.archive.receipts()} == {"work"}
    updated = store.context.dropbox.submit("later.ics", data.replace(b"Planning", b"Updated"))
    await store.context.dropbox.process()
    assert (
        store.context.dropbox.records()["items"][updated["id"]]["parts"][0]["calendar_id"] == "work"
    )


async def test_finalized_attendees_follow_changed_and_cleared_mapping(store):
    await enable(store)
    note = await store.create_note(
        CreateNote(
            title="Mapping change",
            frontmatter={
                "attendees": ["old@example.com"],
                "invitees": ["new@example.com"],
            },
        )
    )
    await store.context.complete_review(
        {
            "note_id": note.id,
            "if_match": note.content_hash,
            "confirmation": "Complete review",
        }
    )
    reviewed = await store.get_note(note.id)
    config = store.context.config()
    for mapped_field, expected in [("invitees", {"new@example.com"}), ("", set())]:
        config.assignments["enrichment"].attendees_field = mapped_field
        store.context.write("config", config.model_dump())
        await store.context.lifecycle.finalize(reviewed)
        assert {p["address"] for p in store.context.read("dossiers", {}).values()} == expected
        assert (await store.get_note(note.id)).content_hash == reviewed.content_hash


async def test_dropbox_archived_parts_are_compact_and_resume_after_interruption(store, monkeypatch):
    import io
    import zipfile

    from coppermind_store.dropbox import Dropbox

    from services.store.tests.test_dropbox import calendar

    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr("work.ics", calendar("work"))
        archive.writestr("family.ics", calendar("family"))
    item = store.context.dropbox.submit("both.zip", zipped.getvalue())
    original_submit = store.context.archive.submit
    calls = 0

    def interrupt(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("interrupted between archived parts")
        return original_submit(*args)

    monkeypatch.setattr(store.context.archive, "submit", interrupt)
    with pytest.raises(OSError):
        await store.context.dropbox.process()
    parts = store.context.dropbox.records()["items"][item["id"]]["parts"]
    assert "data" not in parts[0]
    assert "data" in parts[1]
    monkeypatch.setattr(store.context.archive, "submit", original_submit)
    restarted = Dropbox(store.context)
    await restarted.process()
    for receipt in store.context.archive.receipts():
        assert store.context.archive.process(receipt.id).state == "succeeded"
    await restarted.process()
    records = restarted.records()
    assert records["items"][item["id"]]["state"] == "succeeded"
    assert all("data" not in p for p in records["items"][item["id"]]["parts"])
    assert len(store.context.archive.catalog().events) == 2
    assert (store.context.root / "dropbox-originals" / item["id"]).read_bytes() == zipped.getvalue()
    # Completed receipts from the previous version are compacted too.
    records["items"][item["id"]]["parts"][0]["data"] = "redundant old bytes"
    restarted.save(records)
    await restarted.process()
    assert "data" not in restarted.records()["items"][item["id"]]["parts"][0]


async def test_workflow_setup_preserves_type_choices(store):
    before = store.control.schema()
    key = before.role("type_key")
    choices = next(k.vocabulary for k in before.keys if k.name == key)
    assert choices == ["work", "personal", "reference", "journal"]
    await enable(store)
    await store.context.setup_lifecycle({"revision": store.context.config().revision})
    after = store.control.schema()
    assert next(k.vocabulary for k in after.keys if k.name == key) == choices
