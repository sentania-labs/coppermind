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
        note.id,
        PatchFrontmatter(set={"reviewed": True, "date": "2026-10-16"}, unset=["time"]),
        if_match=note.content_hash,
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


async def test_target_frontmatter_and_stuck_work_note_file_into_month_folder(store, monkeypatch):
    import time

    from coppermind_store import filing, note_lifecycle
    from coppermind_store.reconciler import reconcile_once

    from coppermind.ids import new_id

    await enable(store)
    review_dir = store.notes_root / "Review"
    review_dir.mkdir(parents=True, exist_ok=True)
    target_path = review_dir / "Target work note.md"
    target_path.write_text(
        "---\n"
        "type: work\n"
        "context: external\n"
        "reviewed: true\n"
        "sources: []\n"
        "account: Example Co\n"
        "tags: [upgrade, vcf]\n"
        "date: 2026-10-20T12:00:00\n"
        "---\n"
        "# Target work note\n\n"
        "Reviewed in Obsidian with a date-and-time date.\n",
        encoding="utf-8",
    )

    stuck_id = new_id()
    attendees = [f"person{i}@example.com" for i in range(1, 11)]
    attendees_yaml = "\n".join(f"  - {email}" for email in attendees)
    stuck_path = review_dir / "Customer upgrade call.md"
    stuck_path.write_text(
        "---\n"
        f"id: {stuck_id}\n"
        "schema_version: 1\n"
        "type: work\n"
        "context: external\n"
        "reviewed: true\n"
        "sources: []\n"
        "account: Example Co\n"
        "attendees:\n"
        f"{attendees_yaml}\n"
        "tags: [upgrade, vcf]\n"
        "date: 2026-10-20T12:00:00\n"
        "---\n"
        "# Customer upgrade call\n\n"
        "Notes from the customer upgrade call.\n",
        encoding="utf-8",
    )

    from coppermind.store_protocol import NoteQuery

    await reconcile_once(store)
    notes = (await store.list_notes(NoteQuery())).items
    assert len(notes) == 2
    for item in notes:
        await store.get_note(item.id)
        with pytest.raises(ValueError, match="remain stable"):
            await file_note(store, item.id)

    stable_after = time.monotonic() + 31
    monkeypatch.setattr(note_lifecycle.clock, "monotonic", lambda: stable_after)
    assert await filing.file_reviewed(store) == {"filed": 2, "filing_failed": 0}
    assert await store.get_problems() == []

    filed_stuck = await store.get_note(stuck_id)
    assert filed_stuck.path.startswith("2026-10/")
    assert "time" not in filed_stuck.frontmatter
    assert "timezone" not in filed_stuck.frontmatter
    raw_stuck = (store.notes_root / filed_stuck.path).read_text(encoding="utf-8")
    assert "date: 2026-10-20T12:00:00\n" in raw_stuck

    dossiers = store.context.read("dossiers", {})
    assert {entry["address"] for entry in dossiers.values()} == set(attendees)
    for entry in dossiers.values():
        assert entry["notes"][0]["date"] == "2026-10-20"


async def test_ingest_writes_date_as_a_datetime_and_files_without_edits(store):
    from coppermind_store import filing

    from coppermind.store_protocol import IngestRequest

    await enable(store)
    request = IngestRequest.model_validate(
        {
            "source": {
                "provider": "plaud",
                "external_source_id": "rec-ingest-time-57",
                "source_type": "transcript",
                "captured_at": "2026-10-20T17:00:00Z",
                "artifacts": [
                    {
                        "name": "transcript.txt",
                        "mime_type": "text/plain",
                        "content": "Synthetic transcript for ingest time check.",
                    }
                ],
            },
            "note": {
                "title": "Ingested work call",
                "body": "Summary of the call.\n",
                "frontmatter": {"type": "work", "context": "internal"},
            },
        }
    )
    result = await store.ingest(request)
    note = await store.get_note(result.note.id)
    assert str(note.frontmatter["date"]) == "2026-10-20T12:00:00"
    assert "time" not in note.frontmatter
    assert "timezone" not in note.frontmatter
    raw = (store.notes_root / note.path).read_text(encoding="utf-8")
    assert "date: 2026-10-20T12:00:00\n" in raw

    completed = await store.context.complete_review(
        {"note_id": note.id, "if_match": note.content_hash, "confirmation": "Complete review"}
    )
    assert completed["state"] == "complete"
    assert await filing.file_reviewed(store) == {"filed": 1, "filing_failed": 0}
    filed = await store.get_note(note.id)
    assert filed.path.startswith("2026-10/")


async def test_legacy_date_and_hh_mm_time_keep_filing_and_a_legacy_timezone_is_ignored(store):
    await enable(store)
    legacy = await store.create_note(
        CreateNote(
            title="Legacy work note",
            frontmatter={"type": "work", "date": "2026-10-20", "time": "09:30"},
        )
    )
    await store.context.complete_review(
        {"note_id": legacy.id, "if_match": legacy.content_hash, "confirmation": "Complete review"}
    )
    assert await file_note(store, legacy.id) is True
    assert (await store.get_note(legacy.id)).path.startswith("2026-10/")

    explicit_tz = await store.create_note(
        CreateNote(
            title="Eastern work note",
            frontmatter={
                "type": "work",
                "time": "2026-10-20T12:00:00",
                "timezone": "America/New_York",
            },
        )
    )
    await store.context.complete_review(
        {
            "note_id": explicit_tz.id,
            "if_match": explicit_tz.content_hash,
            "confirmation": "Complete review",
        }
    )
    assert await file_note(store, explicit_tz.id) is True
    assert (await store.get_note(explicit_tz.id)).path.startswith("2026-10/")

    invalid_tz = await store.create_note(
        CreateNote(
            title="Bad timezone note",
            frontmatter={
                "type": "work",
                "time": "2026-10-20T12:00:00",
                "timezone": "Not/AZone",
            },
        )
    )
    await store.context.complete_review(
        {
            "note_id": invalid_tz.id,
            "if_match": invalid_tz.content_hash,
            "confirmation": "Complete review",
        }
    )
    assert await file_note(store, invalid_tz.id) is True

    unknown_with_time = await store.create_note(
        CreateNote(
            title="Time unknown with datetime",
            frontmatter={
                "type": "work",
                "time": "2026-10-20T12:00:00",
                "time_unknown": True,
            },
        )
    )
    await store.context.complete_review(
        {
            "note_id": unknown_with_time.id,
            "if_match": unknown_with_time.content_hash,
            "confirmation": "Complete review",
        }
    )
    assert await file_note(store, unknown_with_time.id) is True
    assert (await store.get_note(unknown_with_time.id)).path.startswith("2026-10/")


async def test_time_with_utc_offset_is_rejected_with_clear_problems_message(store):
    from coppermind_store import filing

    await enable(store)
    note = await store.create_note(
        CreateNote(
            title="Offset time note",
            frontmatter={"type": "work", "time": "2026-10-20T12:00:00-05:00"},
        )
    )
    with pytest.raises(ValueError, match="UTC offset"):
        await store.context.complete_review(
            {"note_id": note.id, "if_match": note.content_hash, "confirmation": "Complete review"}
        )

    note = await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"reviewed": True}), if_match=note.content_hash
    )
    assert await filing.file_reviewed(store) == {"filed": 0, "filing_failed": 1}
    problems = await store.get_problems()
    assert any(
        p.kind == "filing" and p.note_id == note.id and "UTC offset" in p.reason for p in problems
    )


async def test_disagreeing_date_and_time_uses_date_and_flags_date_mismatch_problem(store):
    from coppermind_store import filing, indexer

    from coppermind.store_protocol import NoteQuery, SearchQuery

    await enable(store)
    note = await store.create_note(
        CreateNote(
            title="Mismatch note",
            body="Unique mismatchpalabra content.\n",
            frontmatter={
                "type": "work",
                "date": "2026-09-15",
                "time": "2026-10-20T12:00:00",
                "attendees": ["alex@example.com"],
            },
        )
    )
    await store.context.complete_review(
        {"note_id": note.id, "if_match": note.content_hash, "confirmation": "Complete review"}
    )
    assert await filing.file_reviewed(store) == {"filed": 1, "filing_failed": 0}
    await indexer.catch_up(store)

    filed = await store.get_note(note.id)
    assert filed.path.startswith("2026-09/")
    assert filed.path.endswith("2026-09-15 Mismatch note.md")

    by_range = await store.list_notes(
        NoteQuery.model_validate({"from": "2026-09-15", "to": "2026-09-15"})
    )
    assert [item.id for item in by_range.items] == [note.id]
    await store.field_index.refresh()
    by_field = await store.list_notes(
        NoteQuery(filter='{"all": [{"field": "date", "op": "eq", "value": "2026-09-15"}]}')
    )
    assert [item.id for item in by_field.items] == [note.id]
    old_field = await store.list_notes(
        NoteQuery(filter='{"all": [{"field": "date", "op": "eq", "value": "2026-10-20"}]}')
    )
    assert old_field.items == []
    hits = await store.search(
        SearchQuery.model_validate(
            {"q": "mismatchpalabra", "from": "2026-09-15", "to": "2026-09-15"}
        )
    )
    assert [hit.id for hit in hits.items] == [note.id]

    dossiers = store.context.read("dossiers", {})
    assert next(iter(dossiers.values()))["notes"][0]["date"] == "2026-09-15"

    problems = await store.get_problems()
    mismatch = [p for p in problems if p.kind == "date_mismatch" and p.note_id == note.id]
    assert len(mismatch) == 1
    assert "2026-09-15" in mismatch[0].reason
    assert "2026-10-20" in mismatch[0].reason


async def test_persisted_schema_requiring_date_accepts_time_only_notes(store):
    from coppermind_store import filing

    from coppermind.store_protocol import IngestRequest

    state = store.control.store.read("schema")
    body = dict(state.body)
    for key in body["keys"]:
        if key["name"] == "date":
            key["required"] = True
            key["description"] = "Date of the note (YYYY-MM-DD)."
    store.control.store.write("schema", body, if_revision=state.revision)

    created = await store.create_note(
        CreateNote(
            title="Created under old schema",
            frontmatter={"type": "work", "time": "2026-10-20T12:00:00"},
        )
    )
    assert "date" not in created.frontmatter
    assert created.frontmatter["time"] == "2026-10-20T12:00:00"

    ingested = await store.ingest(
        IngestRequest.model_validate(
            {
                "source": {
                    "provider": "plaud",
                    "external_source_id": "rec-old-schema-57",
                    "source_type": "transcript",
                    "captured_at": "2026-10-20T17:00:00Z",
                    "artifacts": [
                        {
                            "name": "transcript.txt",
                            "mime_type": "text/plain",
                            "content": "Synthetic transcript under old persisted schema.",
                        }
                    ],
                },
                "note": {
                    "title": "Ingested under old schema",
                    "body": "Summary.\n",
                    "frontmatter": {"type": "work", "context": "internal"},
                },
            }
        )
    )
    ingested_note = await store.get_note(ingested.note.id)
    assert str(ingested_note.frontmatter["date"]) == "2026-10-20T12:00:00"
    assert "time" not in ingested_note.frontmatter

    store.control.ensure_defaults()
    date_def = next(k for k in store.control.schema().keys if k.name == "date")
    assert date_def.required is True

    await enable(store)
    await store.context.complete_review(
        {
            "note_id": ingested_note.id,
            "if_match": ingested_note.content_hash,
            "confirmation": "Complete review",
        }
    )
    assert await filing.file_reviewed(store) == {"filed": 1, "filing_failed": 0}
    assert (await store.get_note(ingested_note.id)).path.startswith("2026-10/")


def test_validate_review_rejects_offset_on_any_type_but_malformed_time_only_on_work(store):
    from datetime import UTC, datetime

    from coppermind_store.note_lifecycle import validate_review

    from coppermind.store_protocol import NoteDocument

    schema = store.control.schema()

    def note(**frontmatter):
        return NoteDocument(
            id="01JZ0000000000000000000000",
            path="note.md",
            title="t",
            frontmatter=frontmatter,
            body="",
            content_hash="h",
            size_bytes=0,
            updated_at=datetime.now(UTC),
        )

    validate_review(note(type="personal", date="2026-10-20", time="TBD"), schema)
    with pytest.raises(ValueError, match="valid local meeting time"):
        validate_review(note(type="work", date="2026-10-20", time="TBD"), schema)
    for kind in ("personal", "work"):
        with pytest.raises(ValueError, match="UTC offset"):
            validate_review(note(type=kind, time="2026-10-20T12:00:00-05:00"), schema)
