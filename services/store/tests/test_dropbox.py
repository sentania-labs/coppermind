"""Exports merge automatically; transient receipts never own event history."""

import asyncio
import hashlib
import json
import subprocess
from types import SimpleNamespace
from typing import Any, cast

import pytest
from coppermind_store import calendar_archive, calendar_worker
from coppermind_store import calendar_mirror as mirror
from coppermind_store.calendar_archive import ImportReceipt
from coppermind_store.calendar_import import (
    CalendarImportError,
    CalendarTransientError,
    parse_export,
)
from coppermind_store.context_service import ContextService
from coppermind_store.control import ControlState
from coppermind_store.notes import LocalStore

from coppermind.context_config import CalendarSource
from coppermind.settings import LimitSettings, Wiring
from coppermind.store_protocol import (
    MetadataUnavailable,
    NoteUnparseable,
    NotFound,
    ValidationFailed,
)
from services.store.tests.test_calendar_import import large_ics, zip_bytes


def calendar(uid="", event="event-1", name="Family"):
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\n"
        + (f"UID:{uid}\r\n" if uid else "")
        + f"X-WR-CALNAME:{name}\r\nBEGIN:VEVENT\r\nUID:{event}\r\n"
        "DTSTART:20261016T150000Z\r\nSUMMARY:Planning\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


def local_store(tmp_path):
    wiring = Wiring(data_dir=tmp_path / "data")
    control = ControlState(wiring.state_dir)
    control.ensure_defaults()
    wiring.notes_dir.mkdir(parents=True, exist_ok=True)
    return LocalStore(wiring.notes_dir, control, cast(Any, object()), wiring.sources_dir)


async def finish(store):
    await store.context.dropbox.process()
    for receipt in store.context.archive.receipts():
        if receipt.state == "pending":
            store.context.archive.process(receipt.id)
    await store.context.dropbox.process()


async def test_two_exports_and_two_files_witness_one_event(tmp_path):
    store = local_store(tmp_path)
    first = zip_bytes({"Exchange.ics": calendar(), "copy.ics": calendar()})
    store.context.dropbox.submit("outlook.zip", first)
    await finish(store)
    store.context.dropbox.submit("second.ics", calendar("ignored-calendar-uid"))
    await finish(store)
    catalog = store.context.archive.catalog()
    assert len(catalog.events) == 1
    event = next(iter(catalog.events.values()))
    assert event.calendar_id == "calendar"
    assert len(event.provenance) == 3
    assert {p.filename for p in event.provenance} == {"outlook.zip", "second.ics"}
    assert {p.entry for p in event.provenance} == {"Exchange.ics", "copy.ics", "second.ics"}
    assert all(p.imported_at for p in event.provenance)
    # Reconstruct solely from durable archive originals and receipts.
    (store.context.archive.root / "catalog.json").unlink()
    for receipt in store.context.archive.receipts():
        store.context.archive.process(receipt.id)
    assert (
        next(iter(store.context.archive.catalog().events.values())).provenance == event.provenance
    )


async def test_conflicting_source_keeps_the_stored_copy_and_reports_the_event(tmp_path):
    store = local_store(tmp_path)
    store.context.dropbox.submit("one.ics", calendar())
    await finish(store)
    conflict = store.context.dropbox.submit("two.ics", calendar().replace(b"Planning", b"Conflict"))
    await finish(store)
    receipt = store.context.dropbox.records()["items"][conflict["id"]]
    assert receipt["state"] == "succeeded"
    stored = next(
        r for r in store.context.archive.receipts() if r.id == receipt["parts"][0]["import_id"]
    )
    assert stored.event_count == 0 and stored.kept_count == 1
    assert "title" in stored.conflicts[0].reason
    assert next(iter(store.context.archive.catalog().events.values())).title == "Planning"


async def test_conflict_ends_when_the_event_is_later_revised(tmp_path):
    store = local_store(tmp_path)
    store.context.dropbox.submit("one.ics", calendar())
    await finish(store)
    store.context.dropbox.submit("two.ics", calendar().replace(b"Planning", b"Conflict"))
    await finish(store)
    assert len(mirror.get_conflicts(store.context.archive.engine.connect())) == 1
    newer = calendar().replace(b"Planning", b"Newer").replace(b"SUMMARY", b"SEQUENCE:1\r\nSUMMARY")
    store.context.dropbox.submit("three.ics", newer)
    await finish(store)
    with store.context.archive.engine.connect() as connection:
        assert mirror.get_conflicts(connection) == []


async def test_rejected_version_leaves_no_contact_evidence(tmp_path):
    store = local_store(tmp_path)
    archive = store.context.archive
    attendee = b"ATTENDEE;CN=%b:mailto:%b@example.com\r\n"
    base = calendar().replace(b"SUMMARY", attendee % (b"Casey", b"casey") + b"SUMMARY")
    archive.process(archive.submit("calendar", "base.ics", base).id)
    assert len(archive.catalog().contacts()) == 1
    changed = base.replace(b"SUMMARY", attendee % (b"Dana", b"dana") + b"SUMMARY")
    receipt = archive.process(archive.submit("calendar", "changed.ics", changed).id)
    assert receipt.kept_count == 1
    assert len(archive.catalog().contacts()) == 1


async def test_each_held_conflict_shows_its_own_reason(tmp_path):
    store = local_store(tmp_path)
    archive = store.context.archive
    archive.process(archive.submit("calendar", "base.ics", calendar()).id)
    archive.process(
        archive.submit("calendar", "a.ics", calendar().replace(b"Planning", b"Other")).id
    )
    place = calendar().replace(b"SUMMARY", b"LOCATION:Room 9\r\nSUMMARY")
    archive.process(archive.submit("calendar", "b.ics", place).id)
    rows = (await store.context.snapshot())["calendar"]["conflicts"]
    # Each disagreeing version is one record, keyed by the incoming version, so two
    # versions of one event are two rows that differ in `version` and in their reason.
    assert len(rows) == 2
    assert len({row["version"] for row in rows}) == 2
    kinds = {row["reason"].split(": ", 1)[1].split(".")[0] for row in rows}
    assert kinds == {"title", "location"}
    assert all(row["stored_title"] == "Planning" for row in rows)


async def test_dismissing_failed_receipt_acknowledges_import_failure(tmp_path):
    store = local_store(tmp_path)
    dropbox = store.context.dropbox
    dropbox.submit("one.ics", calendar())
    await finish(store)
    conflict = dropbox.submit("two.ics", calendar().replace(b"Planning", b"Conflict"))
    await finish(store)
    # A conflict is reported per event, so the import itself succeeds.
    assert dropbox.records()["items"][conflict["id"]]["state"] == "succeeded"
    kept = [r for r in store.context.archive.receipts() if r.kept_count]
    assert len(kept) == 1 and kept[0].state == "succeeded" and not kept[0].dismissed
    # A real failure still behaves as before: dismissed, then shown again on re-drop.
    dropbox.submit("bad.ics", b"not a calendar")
    await finish(store)
    failed = [r for r in store.context.archive.receipts() if r.state == "failed"]
    assert [r.dismissed for r in failed] == [False]
    bad = next(
        item for item in dropbox.records()["items"].values() if item["filename"] == "bad.ics"
    )
    await dropbox.dismiss({"id": bad["id"]})
    assert [r.dismissed for r in store.context.archive.receipts() if r.state == "failed"] == [True]
    store.context.archive.dismiss(failed[0].id)
    redrop = dropbox.submit("again.ics", b"not a calendar")
    await finish(store)
    assert dropbox.records()["items"][redrop["id"]]["state"] == "failed"
    assert [r.dismissed for r in store.context.archive.receipts() if r.state == "failed"] == [False]


async def test_retried_receipt_that_succeeds_completes_failed_dropbox_item(tmp_path):
    store = local_store(tmp_path)
    dropbox = store.context.dropbox
    dropbox.submit("one.ics", b"not a calendar")
    await finish(store)
    failed = [r for r in store.context.archive.receipts() if r.state == "failed"]
    assert len(failed) == 1
    item = next(i for i in dropbox.records()["items"].values() if i["filename"] == "one.ics")
    assert item["state"] == "failed"
    newer = calendar().replace(b"Planning", b"Newer").replace(b"SUMMARY", b"SEQUENCE:1\r\nSUMMARY")
    dropbox.submit("two.ics", newer)
    await finish(store)
    assert store.context.archive.process(failed[0].id).state == "failed"
    await dropbox.process()
    # Re-read the item after the retry, so the completed state is the one on record.
    item = next(i for i in dropbox.records()["items"].values() if i["filename"] == "one.ics")
    assert item["state"] == "failed"
    assert item["error"] != ""
    good = next(i for i in dropbox.records()["items"].values() if i["filename"] == "two.ics")
    assert good["state"] == "succeeded"
    assert dropbox.submit("again.ics", newer)["state"] == "already_imported"


async def test_import_history_dismisses_direct_and_dropbox_failures(tmp_path):
    store = local_store(tmp_path)
    context = store.context
    context.archive.process(context.archive.submit("calendar", "first.ics", calendar()).id)
    direct = context.archive.submit("calendar", "direct.ics", b"not a calendar")
    assert context.archive.process(direct.id).state == "failed"
    dropped = context.dropbox.submit("dropped.ics", calendar().replace(b"Planning", b"Dropped"))
    await finish(store)
    # A per-event conflict is not an import failure: the drop succeeds and keeps
    # the stored copy, so only the malformed direct upload is a failed receipt.
    assert context.dropbox.records()["items"][dropped["id"]]["state"] == "succeeded"
    failed = [r for r in context.archive.receipts() if r.state == "failed"]
    assert [r.filename for r in failed] == ["direct.ics"]
    assert await context.dismiss_import({"id": failed[0].id}) == {"dismissed": failed[0].id}
    with pytest.raises(ValueError, match="dismissed"):
        await context.retry_import({"id": failed[0].id})
    assert all(r.dismissed for r in context.archive.receipts() if r.state == "failed")
    snapshot = await context.snapshot()
    assert {r["filename"] for r in snapshot["calendar"]["imports"] if r["dismissed"]} == {
        "direct.ics",
    }
    succeeded = next(r for r in context.archive.receipts() if r.state == "succeeded")
    with pytest.raises(ValueError, match="Only failed"):
        await context.dismiss_import({"id": succeeded.id})
    with pytest.raises(ValueError, match="Unknown"):
        await context.dismiss_import({"id": "0" * 64})


async def test_dismissing_failed_import_before_reconcile_clears_importing_item(tmp_path):
    store = local_store(tmp_path)
    context = store.context
    context.archive.process(context.archive.submit("calendar", "first.ics", calendar()).id)
    dropped = context.dropbox.submit("late.ics", b"not a calendar")
    await context.dropbox.process()
    assert context.dropbox.records()["items"][dropped["id"]]["state"] == "importing"
    receipt = next(r for r in context.archive.receipts() if r.state == "pending")
    assert context.archive.process(receipt.id).state == "failed"
    await context.dismiss_import({"id": receipt.id})
    assert dropped["id"] not in context.dropbox.records()["items"]
    await context.dropbox.process()
    assert context.dropbox.snapshot()["items"] == []
    assert context.dropbox.records()["history"][dropped["id"]]["state"] == "failed"


async def test_dismiss_expiry_redrop_and_failed_receipt_retention(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    dropbox = store.context.dropbox
    item = dropbox.submit("one.ics", calendar())
    await finish(store)
    await dropbox.dismiss({"id": item["id"]})
    assert dropbox.snapshot()["items"] == []
    again = dropbox.submit("renamed.ics", calendar())
    assert again["state"] == "already_imported"
    await finish(store)
    assert len(store.context.archive.receipts()) == 1
    bad = dropbox.submit("bad.ics", b"bad")
    await finish(store)
    future = again["completed_at"] + 604801
    monkeypatch.setattr("coppermind_store.dropbox.time.time", lambda: future)
    await dropbox.process()
    assert [i["id"] for i in dropbox.snapshot()["items"]] == [bad["id"]]
    assert item["id"] in dropbox.records()["history"]
    await dropbox.dismiss({"id": bad["id"]})
    assert dropbox.snapshot()["items"] == []
    assert len(store.context.archive.receipts()) == 2


async def test_oversize_file_failure_keeps_changes_saved_while_scan_waited(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    root = store.notes_root / "Dropbox"
    root.mkdir()
    (root / "huge.zip").write_bytes(b"x" * 64)
    monkeypatch.setattr("coppermind_store.dropbox.MAX_UPLOAD", 8)
    clock = [0.0]
    monkeypatch.setattr("coppermind_store.dropbox.time.monotonic", lambda: clock[0])
    dropbox = store.context.dropbox
    await dropbox.scan()
    clock[0] = 31
    async with store.context.lock:
        task = asyncio.create_task(dropbox.scan())
        for _ in range(5):
            await asyncio.sleep(0)
        records = dropbox.records()
        records["items"]["concurrent"] = {
            "id": "concurrent",
            "filename": "later.ics",
            "state": "failed",
            "parts": [],
            "error": "",
        }
        dropbox.save(records)
    await task
    items = dropbox.records()["items"]
    assert "concurrent" in items
    assert {item["filename"] for item in items.values()} == {"huge.zip", "later.ics"}


async def test_scanner_logs_and_dismissal_survive_restart(tmp_path, monkeypatch, caplog):
    store = local_store(tmp_path)
    root = store.notes_root / "Dropbox"
    root.mkdir()
    path = root / "outlook.zip"
    original = zip_bytes({"calendar.ics": calendar()})
    path.write_bytes(original)
    clock = [0.0]
    monkeypatch.setattr("coppermind_store.dropbox.time.monotonic", lambda: clock[0])
    with caplog.at_level("INFO"):
        await store.context.dropbox.scan()
        clock[0] = 31
        await store.context.dropbox.scan()
        await finish(store)
        item = store.context.dropbox.snapshot()["items"][0]
        await store.context.dropbox.dismiss({"id": item["id"]})
        store.context = ContextService(store)
        await store.context.dropbox.scan()
        assert store.context.dropbox.snapshot()["items"] == []
        assert not path.exists()
        await store.context.dropbox.scan()
        path.write_bytes(original)
        await store.context.dropbox.scan()
        clock[0] = 62
        await store.context.dropbox.scan()
    assert store.context.dropbox.snapshot()["items"][0]["state"] == "already_imported"
    assert "Dropbox park" in caplog.text
    assert "Dropbox adopt" in caplog.text
    assert "Dropbox skip" in caplog.text


async def test_uncompressed_exchange_zip_over_10_mib_imports(tmp_path):
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Exchange.ics", large_ics(""))
    data = buffer.getvalue()
    assert len(data) > 10 * 1024**2
    store = local_store(tmp_path)
    store.context.dropbox.submit("Exchange.zip", data)
    await finish(store)
    assert len(store.context.archive.catalog().events) == 12000
    assert store.context.dropbox.snapshot()["items"][0]["state"] == "succeeded"


class LegacyCatalog:
    """The schema-1 per-calendar catalog file written before one-calendar intake."""

    def __init__(self):
        self.events: dict[str, Any] = {}
        self.imports: list[str] = []

    def model_dump_json(self):
        return json.dumps(
            {
                "schema_version": 1,
                "events": {k: json.loads(v.model_dump_json()) for k, v in self.events.items()},
                "imports": self.imports,
            }
        )


async def test_zip_expanding_past_40_mib_fails_the_receipt(tmp_path):
    store = local_store(tmp_path)
    source = zip_bytes({f"{n}.ics": large_ics(str(n)) for n in range(4)})
    item = store.context.dropbox.submit("export.zip", source)
    await finish(store)
    record = store.context.dropbox.records()["items"][item["id"]]
    assert record["state"] == "failed"
    assert "expanded archive exceeds 41.94 MB" in record["error"]


def legacy_archive(store):
    archive = store.context.archive
    old = LegacyCatalog()
    for source in ("work", "personal"):
        data = calendar(source)
        digest = hashlib.sha256(data).hexdigest()
        identity = hashlib.sha256(f"{source}\0.ics\0{digest}".encode()).hexdigest()
        receipt = ImportReceipt(
            id=identity,
            calendar_id=source,
            filename=source + ".ics",
            content_hash=digest,
            received_at="2026-10-01T00:00:00+00:00",
            state="succeeded",
        )
        archive._save_receipt(receipt)
        (archive.root / "imports" / identity / "original").write_bytes(data)
        event = parse_export(source, receipt.filename, data)[0]
        old.events[event.identity] = event
        old.imports.append(identity)
    (archive.root / "catalog.json").write_text(old.model_dump_json())
    config = store.context.config()
    config.sources = [CalendarSource(id="work", name="Old work", folder="Calendars/Work")]
    store.context.write("config", config.model_dump())
    (store.context.root / "one-calendar-migration.json").unlink()
    return old


def test_legacy_calendars_merge_once_preserving_provenance_and_backup(tmp_path):
    store = local_store(tmp_path)
    old = legacy_archive(store)
    store.context = ContextService(store)
    catalog = store.context.archive.catalog()
    assert len(catalog.events) == 1
    event = next(iter(catalog.events.values()))
    assert {p.legacy_calendar for p in event.provenance} == {"Old work", "personal"}
    assert all(catalog.aliases[key] == event.identity for key in old.events)
    assert store.context.config().sources == []
    backup = store.context.archive.root / "before-one-calendar.json"
    assert json.loads(backup.read_bytes())["events"].keys() == old.events.keys()
    before = (store.context.archive.root / "catalog.json").read_bytes()
    store.context = ContextService(store)
    assert (store.context.archive.root / "catalog.json").read_bytes() == before


def test_migration_survives_unparseable_original_and_keeps_legacy_provenance(tmp_path):
    store = local_store(tmp_path)
    old = legacy_archive(store)
    receipt = store.context.archive.receipts()[0]
    (store.context.archive.root / "imports" / receipt.id / "original").write_bytes(b"corrupt")
    store.context = ContextService(store)
    catalog = store.context.archive.catalog()
    assert (
        json.loads((store.context.archive.root / "catalog.json").read_bytes())["schema_version"]
        == 2
    )
    assert len(catalog.events) == 1
    event = next(iter(catalog.events.values()))
    assert {p.legacy_calendar for p in event.provenance} <= {"Old work", "personal"}
    assert event.provenance
    assert all(catalog.aliases[key] == event.identity for key in old.events)


def test_migration_retains_conflicting_legacy_version_and_unrelated_events(tmp_path):
    store = local_store(tmp_path)
    old = legacy_archive(store)
    events = list(old.events.values())
    events[0].title = "Conflicting"
    events[0].component = events[0].component.replace("Planning", "Conflicting")
    (store.context.archive.root / "catalog.json").write_text(old.model_dump_json())
    store.context = ContextService(store)
    catalog = store.context.archive.catalog()
    assert len(catalog.events) == len(catalog.conflicts) == 1
    assert {next(iter(catalog.events.values())).title, catalog.conflicts[0].title} == {
        "Planning",
        "Conflicting",
    }


async def test_pending_receipt_cannot_be_dismissed(tmp_path):
    store = local_store(tmp_path)
    item = store.context.dropbox.submit("one.ics", calendar())
    with pytest.raises(ValueError, match="completed or failed"):
        await store.context.dropbox.dismiss({"id": item["id"]})


async def test_upgrade_resumes_unclaimed_exchange_export(tmp_path):
    store = local_store(tmp_path)
    item = store.context.dropbox.submit("exchange.zip", zip_bytes({"Exchange.ics": calendar()}))
    records = store.context.dropbox.records()
    records["items"][item["id"]]["state"] = "needs_identity"
    records["items"][item["id"]]["parts"] = [{"calendar_id": "", "entry": "Exchange.ics"}]
    records["calendars"] = {}
    store.context.dropbox.save(records)
    (store.context.root / "one-calendar-migration.json").unlink()
    store.context = ContextService(store)
    await finish(store)
    assert store.context.dropbox.snapshot()["items"][0]["state"] == "succeeded"
    assert len(store.context.archive.catalog().events) == 1


async def test_direct_upload_then_drop_is_already_imported(tmp_path):
    store = local_store(tmp_path)
    receipt = store.context.archive.submit("calendar", "first.ics", calendar())
    store.context.archive.process(receipt.id)
    item = store.context.dropbox.submit("same.ics", calendar())
    assert item["state"] == "already_imported"
    await finish(store)
    assert len(store.context.archive.receipts()) == 1


async def test_migrated_api_shape_and_original_replay_keep_provenance(tmp_path):
    store = local_store(tmp_path)
    legacy_archive(store)
    store.context = ContextService(store)
    snapshot = await store.context.snapshot()
    assert snapshot["calendar"]["id"] == "calendar"
    assert {r["calendar_id"] for r in snapshot["calendar"]["imports"]} == {"calendar"}
    assert "sources" not in snapshot["config"]
    assert "calendars" not in snapshot["dropbox"]
    (store.context.archive.root / "catalog.json").unlink()
    for receipt in store.context.archive.receipts():
        store.context.archive.process(receipt.id)
    event = next(iter(store.context.archive.catalog().events.values()))
    assert {p.legacy_calendar for p in event.provenance} == {"Old work", "personal"}


async def test_waiting_receipt_tracks_changes_and_disappears_when_file_removed(
    tmp_path, monkeypatch
):
    store = local_store(tmp_path)
    folder = store.notes_root / "Dropbox"
    folder.mkdir()
    path = folder / "Exchange.ics"
    path.write_bytes(calendar())
    clock = [0.0]
    monkeypatch.setattr("coppermind_store.dropbox.time.monotonic", lambda: clock[0])
    await store.context.dropbox.scan()
    assert store.context.dropbox.snapshot()["items"][0]["state"] == "stabilizing"
    clock[0] = 31
    await store.context.tick()
    item = store.context.dropbox.snapshot()["items"][0]
    assert item["state"] == "succeeded"  # Reconciled during the same tick.
    assert item["event_count"] == 1
    first_imported = item["first_imported_at"]
    assert first_imported
    await store.context.dropbox.dismiss({"id": item["id"]})
    store.context.dropbox.submit("again.ics", calendar())
    assert store.context.dropbox.snapshot()["items"][0]["first_imported_at"] == first_imported
    path.write_bytes(calendar(event="changed"))
    await store.context.dropbox.scan()
    assert any(i["state"] == "stabilizing" for i in store.context.dropbox.snapshot()["items"])
    path.unlink()
    await store.context.dropbox.scan()
    assert all(i["state"] != "stabilizing" for i in store.context.dropbox.snapshot()["items"])


async def test_calendar_history_dismisses_direct_and_dropbox_failures(tmp_path):
    store = local_store(tmp_path)
    archive = store.context.archive
    archive.process(archive.submit("calendar", "base.ics", calendar()).id)
    bad = calendar().replace(b"Planning", b"Wrong title")
    direct = archive.process(archive.submit("calendar", "direct.ics", bad).id)
    store.context.dropbox.submit("redrop.zip", zip_bytes({"Work/Meeting.ics": bad}))
    await finish(store)
    # Both imports land: each reports its one conflicting event and keeps the stored copy.
    assert direct.state == "succeeded" and direct.kept_count == 1
    zipped = next(r for r in archive.receipts() if r.filename.endswith("zip"))
    assert zipped.state == "succeeded" and zipped.kept_count == 1
    assert store.context.dropbox.snapshot()["items"][0]["state"] == "succeeded"
    reason = zipped.conflicts[0].reason
    for detail in ("Wrong title", "event-1", "Work/Meeting.ics", "title", "stored version"):
        assert detail in reason
    conflicts = archive.catalog().conflicts
    # Two imports of the same bytes disagree with the stored event in the same way,
    # so the one disagreeing version is recorded once.
    assert len(conflicts) == 1
    assert {event.title for event in conflicts} == {"Planning"}
    snapshot = await store.context.snapshot()
    assert len(snapshot["calendar"]["conflicts"]) == 1
    assert all(item["reason"] for item in snapshot["calendar"]["conflicts"])
    assert {item["stored_title"] for item in snapshot["calendar"]["conflicts"]} == {"Planning"}
    assert {item["id"] for item in snapshot["calendar"]["conflicts"]} == {
        conflict.identity for conflict in conflicts
    }
    await store.context.dismiss_calendar_conflict(
        {
            "id": conflicts[0].identity,
            "version": snapshot["calendar"]["conflicts"][0]["version"],
        }
    )
    assert len(archive.catalog().conflicts) == 0
    assert next(iter(archive.catalog().events.values())).title == "Planning"


async def test_an_acknowledged_conflict_stays_quiet_on_reimport(tmp_path):
    store = local_store(tmp_path)
    archive = store.context.archive
    archive.process(archive.submit("calendar", "base.ics", calendar()).id)
    wrong = calendar().replace(b"Planning", b"Wrong title")
    first = archive.process(archive.submit("calendar", "one.ics", wrong).id)
    assert first.kept_count == 1 and len(first.conflicts) == 1
    held = archive.catalog().conflicts
    assert len(held) == 1

    await store.context.dismiss_calendar_conflict(
        {"id": held[0].identity, "version": held[0].conflict_version}
    )
    assert archive.catalog().conflicts == []

    reexport = wrong.replace(b"SUMMARY", b"DTSTAMP:20261001T000000Z\r\nSUMMARY")
    again = archive.process(archive.submit("calendar", "two.ics", reexport).id)
    assert again.state == "succeeded" and again.conflicts == []
    assert archive.catalog().conflicts == []
    assert (await store.context.snapshot())["calendar"]["conflicts"] == []

    third = calendar().replace(b"Planning", b"Another title")
    latest = archive.process(archive.submit("calendar", "three.ics", third).id)
    assert len(latest.conflicts) == 1
    assert [event.identity for event in archive.catalog().conflicts] == [held[0].identity]
    assert next(iter(archive.catalog().events.values())).title == "Planning"


def test_migration_drops_superseded_conflicts_and_allows_acknowledgement(tmp_path):
    store = local_store(tmp_path)
    archive = store.context.archive
    old = LegacyCatalog()
    # Visit revisions 0, 0, 1 in old-identity order, the order migration uses.
    sources = sorted(
        ("work", "personal", "third"),
        key=lambda name: parse_export(name, "old.ics", calendar())[0].identity,
    )
    for index, source in enumerate(sources):
        data = calendar(source).replace(
            b"SUMMARY", f"SEQUENCE:{int(index == 2)}\r\nSUMMARY".encode()
        )
        if index == 1:
            data = data.replace(b"Planning", b"Conflicting")
        event = parse_export(source, source + ".ics", data)[0]
        old.events[event.identity] = event
        digest = hashlib.sha256(data).hexdigest()
        receipt = ImportReceipt(
            id=digest,
            calendar_id=source,
            filename=source + ".ics",
            content_hash=digest,
            received_at="2026-10-01T00:00:00+00:00",
            state="succeeded",
        )
        archive._save_receipt(receipt)
        (archive.root / "imports" / receipt.id / "original").write_bytes(data)
        old.imports.append(receipt.id)
    second = list(old.events.values())[1]
    (archive.root / "catalog.json").write_text(old.model_dump_json())
    (store.context.root / "one-calendar-migration.json").unlink()
    store.context = ContextService(store)
    assert store.context.archive.catalog().conflicts == []
    archive = store.context.archive
    before = dict(archive.catalog().events.items())
    conflict = second.model_copy(update={"calendar_id": "calendar"})
    with archive.engine.begin() as connection:
        mirror.put_conflicts(connection, [conflict])
        mirror.write_catalog(
            connection, archive.root / "catalog.json", list(archive.catalog().imports)
        )
    assert archive.catalog().conflicts == [conflict]
    archive.dismiss_conflict(conflict.identity, conflict.semantic_hash)
    assert archive.catalog().conflicts == []
    assert dict(archive.catalog().events.items()) == before


async def test_unreadable_names_fail_visibly_without_blocking_other_intake(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    root = store.notes_root / "Dropbox"
    root.mkdir()
    (root / "a\tb.ics").write_bytes(calendar())
    (root / "good.ics").write_bytes(calendar(event="event-2"))
    clock = [0.0]
    monkeypatch.setattr("coppermind_store.dropbox.time.monotonic", lambda: clock[0])
    dropbox = store.context.dropbox
    await dropbox.scan()
    clock[0] = 31
    await dropbox.scan()
    await dropbox.scan()
    await finish(store)
    states = {item["filename"]: item for item in dropbox.snapshot()["items"]}
    assert states["a\tb.ics"]["state"] == "failed"
    assert "invalid filename" in states["a\tb.ics"]["error"]
    assert states["good.ics"]["state"] == "succeeded"
    await dropbox.dismiss({"id": states["a\tb.ics"]["id"]})
    assert "a\tb.ics" not in {item["filename"] for item in dropbox.snapshot()["items"]}


async def test_redrop_retries_a_transient_import_failure(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    archive = store.context.archive
    real = calendar_archive.parse_bounded

    def slow(*args, **kwargs):
        raise CalendarTransientError("calendar processing exceeded its time limit")

    monkeypatch.setattr("coppermind_store.calendar_archive.parse_bounded", slow)
    first = store.context.dropbox.submit("big.ics", calendar())
    await finish(store)
    failed = store.context.dropbox.records()["items"][first["id"]]
    assert failed["state"] == "failed"
    assert failed["error"].endswith("time limit. Drop the export again to retry.")
    assert archive.receipts()[0].retryable
    assert store.context.dropbox.snapshot()["items"][0]["retryable"]

    monkeypatch.setattr("coppermind_store.calendar_archive.parse_bounded", real)
    again = store.context.dropbox.submit("big.ics", calendar())
    assert again["state"] == "pending"
    await finish(store)
    item = store.context.dropbox.records()["items"][first["id"]]
    assert item["state"] == "succeeded" and item["error"] == ""
    assert [r.state for r in archive.receipts()] == ["succeeded"]
    assert len(archive.catalog().events) == 1


@pytest.mark.parametrize("kind", ["malformed", "conflict"])
async def test_redrop_of_deterministic_failure_reprocesses_into_the_same_receipt(tmp_path, kind):
    store = local_store(tmp_path)
    archive = store.context.archive
    archive.process(archive.submit("calendar", "base.ics", calendar()).id)
    # Same revision, different contents: the import succeeds and reports the
    # one conflicting event, so the deterministic answer is a kept count.
    data = b"not a calendar" if kind == "malformed" else calendar().replace(b"Planning", b"Other")
    store.context.dropbox.submit("bad.ics", data)
    await finish(store)
    receipt = next(r for r in archive.receipts() if r.filename == "bad.ics")
    expected = "failed" if kind == "malformed" else "succeeded"
    assert receipt.state == expected and not receipt.retryable
    if kind == "malformed":
        assert "Drop the export again to retry" not in receipt.error
        assert not store.context.dropbox.snapshot()["items"][0]["retryable"]

    store.context.dropbox.submit("bad.ics", data)
    await finish(store)
    receipts = [r for r in archive.receipts() if r.filename == "bad.ics"]
    assert [(r.id, r.state, r.error) for r in receipts] == [
        (receipt.id, receipt.state, receipt.error)
    ]
    if kind == "conflict":
        assert receipt.kept_count == 1
        assert len(receipts) == 1


def test_worker_timeout_and_crash_are_transient_but_worker_errors_are_not(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("worker", 45)

    monkeypatch.setattr(calendar_worker.subprocess, "run", timeout)
    with pytest.raises(CalendarTransientError):
        calendar_worker.isolated_calendar({})
    crashed = subprocess.CompletedProcess([], 1, b"", b"")
    monkeypatch.setattr(calendar_worker.subprocess, "run", lambda *a, **k: crashed)
    with pytest.raises(CalendarTransientError):
        calendar_worker.isolated_calendar({})
    refused = subprocess.CompletedProcess([], 0, b'{"error": "invalid ICS document"}', b"")
    monkeypatch.setattr(calendar_worker.subprocess, "run", lambda *a, **k: refused)
    with pytest.raises(CalendarImportError) as raised:
        calendar_worker.isolated_calendar({})
    assert not isinstance(raised.value, CalendarTransientError)


def fake_notes(store, notes, monkeypatch, fail=None):
    fail = fail or {}
    patched = []

    async def get_note(note_id):
        if note_id not in notes:
            raise NotFound(note_id)
        return SimpleNamespace(
            id=note_id, frontmatter=dict(notes[note_id]), content_hash="hash-" + note_id
        )

    async def patch_frontmatter(note_id, request, if_match):
        if note_id in fail:
            raise fail[note_id]
        assert if_match == "hash-" + note_id and not request.unset
        notes[note_id].update(request.set)
        patched.append((note_id, request.set))

    monkeypatch.setattr(store, "get_note", get_note)
    monkeypatch.setattr(store, "patch_frontmatter", patch_frontmatter)
    return patched


async def test_upgrade_rewrites_legacy_event_ids_in_notes_and_records(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    old = legacy_archive(store)
    config = store.context.config()
    config.assignments["enrichment"].event_field = "calendar_event"
    store.context.write("config", config.model_dump())
    old_id = next(iter(old.events))
    store.context = ContextService(store)
    aliases = store.context.archive.catalog().aliases
    new_id = aliases[old_id]
    assert new_id != old_id
    store.context.write(
        "associations",
        {
            "n1": {"events": [old_id, "other"], "contacts": ["c"], "attendees": []},
            "n2": {"events": [new_id], "contacts": [], "attendees": []},
        },
    )
    store.context.write(
        "finalized",
        {"n1": {"confirmed_calendar": {"event_id": old_id, "at": "t"}}, "n3": {}},
    )
    notes = {
        "n1": {"calendar_event": old_id, "title": "keep"},
        "n2": {"calendar_event": new_id},
        "n3": {"calendar_event": old_id},
    }
    patched = fake_notes(
        store, notes, monkeypatch, fail={"n3": MetadataUnavailable("database down")}
    )

    await store.context.migrate_event_ids()
    assert patched == [("n1", {"calendar_event": new_id})]
    assert store.context.read("associations", {})["n1"]["events"] == [old_id, "other"]
    assert not store.context.read("one-calendar-event-ids", {}).get("complete")

    patched_failure = fake_notes(store, notes, monkeypatch)
    await store.context.migrate_event_ids()
    assert patched_failure == [("n3", {"calendar_event": new_id})]
    associations = store.context.read("associations", {})
    assert associations["n1"]["events"] == sorted([new_id, "other"])
    assert associations["n2"]["events"] == [new_id]
    assert store.context.read("finalized", {})["n1"]["confirmed_calendar"]["event_id"] == new_id
    assert notes["n1"] == {"calendar_event": new_id, "title": "keep"}
    assert (store.context.root / "before-one-calendar-event-ids.json").exists()
    assert store.context.archive.catalog().aliases == aliases

    patched_failure.clear()
    await store.context.migrate_event_ids()
    assert patched_failure == []


async def test_upgrade_skips_notes_that_can_never_be_patched_and_finishes(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    old = legacy_archive(store)
    config = store.context.config()
    config.assignments["enrichment"].event_field = "calendar_event"
    store.context.write("config", config.model_dump())
    old_id = next(iter(old.events))
    store.context = ContextService(store)
    new_id = store.context.archive.catalog().aliases[old_id]
    store.context.write(
        "associations",
        {
            name: {"events": [old_id], "contacts": [], "attendees": []}
            for name in ("n1", "n2", "n3")
        },
    )
    notes = {name: {"calendar_event": old_id} for name in ("n1", "n2", "n3")}
    patched = fake_notes(
        store,
        notes,
        monkeypatch,
        fail={"n1": ValidationFailed(["bad field"]), "n2": NoteUnparseable("n2", "yaml")},
    )
    await store.context.migrate_event_ids()
    assert patched == [("n3", {"calendar_event": new_id})]
    assert store.context.read("one-calendar-event-ids", {}).get("complete")
    associations = store.context.read("associations", {})
    assert all(record["events"] == [new_id] for record in associations.values())
    patched.clear()
    await store.context.migrate_event_ids()
    assert patched == []


async def test_unsupported_receipt_can_be_dismissed(tmp_path):
    store = local_store(tmp_path)
    dropbox = store.context.dropbox
    item = dropbox.submit("notes.unknown", b"not a supported file")
    await finish(store)
    assert dropbox.snapshot()["items"][0]["state"] == "unsupported"
    assert await dropbox.dismiss({"id": item["id"]}) == {"dismissed": item["id"]}
    assert dropbox.snapshot()["items"] == []
    assert dropbox.records()["history"][item["id"]]["state"] == "unsupported"


async def test_a_kind_marker_routes_a_drop_to_the_artifact_store(tmp_path):
    """A `.kind` sidecar saying artifact is the only way a non-calendar drop is taken."""
    from coppermind.store_protocol import CreatedArtifact

    store = local_store(tmp_path)
    requests = []

    async def create_artifact(request, *, payload_size_bytes=None):
        requests.append(request)
        return CreatedArtifact(
            id="01K4Q8Z2A0P1Q2R3S4T5U6V7W8",
            revision=1,
            created=True,
            path="Artifacts/2026/2026-11/deck.pptx",
            title="Deck",
        )

    store.create_artifact = create_artifact
    marked = store.context.dropbox.submit(
        "deck.pptx", b"PK\x03\x04deck", {"kind": "artifact", "title": "Deck", "tags": ["reference"]}
    )
    assert marked["state"] == "pending" and marked["kind"]["kind"] == "artifact"
    await store.context.dropbox.process()
    item = store.context.dropbox.snapshot()["items"][0]
    assert item["state"] == "succeeded"
    assert item["parts"] == [
        {"artifact_id": "01K4Q8Z2A0P1Q2R3S4T5U6V7W8", "path": "Artifacts/2026/2026-11/deck.pptx"}
    ]
    assert requests[0].kind == "artifact"
    assert requests[0].filename == "deck.pptx"
    assert requests[0].origin == "dropbox"
    assert requests[0].title == "Deck"
    assert requests[0].tags == ["reference"]
    assert requests[0].bytes() == b"PK\x03\x04deck"


async def test_an_unmarked_non_calendar_drop_is_parked_with_the_kind_instruction(tmp_path):
    store = local_store(tmp_path)
    store.context.dropbox.submit("deck.unknown", b"unknown format")
    await store.context.dropbox.process()
    item = store.context.dropbox.snapshot()["items"][0]
    assert item["state"] == "unsupported"
    assert "not supported" in item["error"]
    assert "supported format" in item["error"]
    assert (store.context.root / "dropbox-originals" / item["id"]).exists()


async def test_an_unmarked_document_upload_is_told_to_declare_the_artifact_kind(tmp_path):
    store = local_store(tmp_path)
    store.context.dropbox.submit("report.pdf", b"%PDF-1.4 report")
    await store.context.dropbox.process()
    item = store.context.dropbox.snapshot()["items"][0]
    assert item["state"] == "unsupported"
    assert "kind=artifact" in item["error"]
    assert "not supported" not in item["error"]


async def test_a_long_multibyte_filename_still_moves_to_failed(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    root = store.notes_root / "Dropbox"
    root.mkdir()
    path = root / ("\u6f22" * 80 + ".xyz")
    path.write_bytes(b"unknown")
    clock = [0.0]
    monkeypatch.setattr("coppermind_store.dropbox.time.monotonic", lambda: clock[0])
    dropbox = store.context.dropbox
    await dropbox.scan()
    clock[0] = 31
    await dropbox.scan()
    await dropbox.process()
    await dropbox.scan()
    assert not path.exists()
    files = list((root / "Failed").iterdir())
    assert len(files) == 2
    assert all(len(p.name.encode()) <= 255 for p in files)
    assert (root / "Failed" / path.name).read_bytes() == b"unknown"


def test_submit_refuses_a_marker_that_is_not_artifact(tmp_path):
    store = local_store(tmp_path)
    with pytest.raises(ValueError, match="only artifact"):
        store.context.dropbox.submit("x.pptx", b"PK", {"kind": "note"})


async def test_the_folder_scan_pairs_a_sidecar_with_its_file(tmp_path, monkeypatch):
    """The sidecar itself is never a drop, and the file beside it carries the marker."""
    store = local_store(tmp_path)
    folder = store.notes_root / store.context.config().dropbox_folder
    folder.mkdir(parents=True)
    (folder / "deck.pptx").write_bytes(b"PK\x03\x04deck")
    (folder / "deck.pptx.kind").write_text("artifact\n", encoding="utf-8")
    submitted = []

    def submit(filename, data, kind=None):
        submitted.append((filename, data, kind))
        return {"id": "x", "state": "pending", "parts": [], "error": "", "filename": filename}

    monkeypatch.setattr(store.context.dropbox, "submit", submit)
    clock = iter([0.0, 100.0, 100.0, 100.0, 200.0, 200.0, 200.0, 200.0])
    monkeypatch.setattr("coppermind_store.dropbox.time.monotonic", lambda: next(clock, 300.0))
    await store.context.dropbox.scan()
    await store.context.dropbox.scan()
    assert submitted == [("deck.pptx", b"PK\x03\x04deck", {"kind": "artifact"})]


async def test_a_marked_redrop_is_stored_again_after_the_artifact_was_removed(tmp_path):
    from coppermind.store_protocol import CreatedArtifact

    store = local_store(tmp_path)
    stored = []

    async def create_artifact(request, *, payload_size_bytes=None):
        stored.append(request)
        return CreatedArtifact(
            id=f"01K4Q8Z2A0P1Q2R3S4T5U6V7W{len(stored)}",
            revision=1,
            created=True,
            path="Artifacts/2026/2026-11/deck.pptx",
            title="Deck",
        )

    store.create_artifact = create_artifact
    dropbox = store.context.dropbox
    marker = {"kind": "artifact"}
    first = dropbox.submit("deck.pptx", b"PK\x03\x04deck", marker)
    await dropbox.process()
    assert dropbox.snapshot()["items"][0]["state"] == "succeeded"
    assert dropbox.records()["history"][first["id"]]["state"] == "succeeded"
    again = dropbox.submit("deck.pptx", b"PK\x03\x04deck", marker)
    assert again["state"] == "pending"
    await dropbox.process()
    assert len(stored) == 2
    assert dropbox.snapshot()["items"][0]["state"] == "succeeded"
    assert dropbox.submit("deck.pptx", b"PK\x03\x04deck")["state"] == "already_imported"


@pytest.mark.parametrize(
    "filename,data", [("report.pdf", b"%PDF-test"), ("deck.pptx", b"PK\x03\x04deck")]
)
async def test_document_drops_are_archived_without_a_marker(tmp_path, filename, data):
    from coppermind.store_protocol import CreatedArtifact

    store = local_store(tmp_path)
    requests = []

    async def create_artifact(request, *, payload_size_bytes=None):
        requests.append(request)
        return CreatedArtifact(
            id="01K4Q8Z2A0P1Q2R3S4T5U6V7W8",
            revision=1,
            created=True,
            path="Artifacts/file",
            title="File",
        )

    store.create_artifact = create_artifact
    root = store.notes_root / "Dropbox"
    root.mkdir()
    (root / filename).write_bytes(data)
    store.context.dropbox.seen[filename] = ((0, 0, 0, -1), 0)  # replaced by first scan
    await store.context.dropbox.scan()
    store.context.dropbox.seen[filename] = (store.context.dropbox.seen[filename][0], 0)
    await store.context.dropbox.scan()
    item = next(iter(store.context.dropbox.records()["items"].values()))
    await store.context.dropbox.process()
    assert store.context.dropbox.records()["items"][item["id"]]["state"] == "succeeded"
    assert requests[0].filename == filename and requests[0].bytes() == data


async def test_unsupported_drop_moves_to_failed_with_reason(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    root = store.notes_root / "Dropbox"
    root.mkdir()
    path = root / "unknown.xyz"
    path.write_bytes(b"unknown")
    clock = [0.0]
    monkeypatch.setattr("coppermind_store.dropbox.time.monotonic", lambda: clock[0])
    dropbox = store.context.dropbox
    await dropbox.scan()
    clock[0] = 31
    await dropbox.scan()
    await dropbox.process()
    await dropbox.scan()
    assert not path.exists()
    files = list((root / "Failed").iterdir())
    assert len(files) == 2
    original = next(p for p in files if not p.name.endswith(".reason.txt"))
    assert original.read_bytes() == b"unknown"
    assert "not supported" in original.with_name(original.name + ".reason.txt").read_text()
    assert "Find the file in Dropbox/Failed" in dropbox.snapshot()["items"][0]["error"]
    await dropbox.scan()
    assert len(list((root / "Failed").iterdir())) == 2


async def test_changed_drop_is_not_removed_after_import(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    root = store.notes_root / "Dropbox"
    root.mkdir()
    path = root / "calendar.ics"
    path.write_bytes(calendar())
    clock = [0.0]
    monkeypatch.setattr("coppermind_store.dropbox.time.monotonic", lambda: clock[0])
    await store.context.dropbox.scan()
    clock[0] = 31
    await store.context.dropbox.scan()
    await finish(store)
    changed = calendar(event="changed-event")
    path.write_bytes(changed)
    await store.context.dropbox.scan()
    assert path.read_bytes() == changed


async def settle(dropbox, clock):
    clock[0] += 31
    await dropbox.scan()
    clock[0] += 31
    await dropbox.scan()
    await dropbox.process()
    await dropbox.scan()


async def test_failed_files_keep_their_name_and_a_taken_name_gets_a_folder(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    root = store.notes_root / "Dropbox"
    root.mkdir()
    clock = [0.0]
    monkeypatch.setattr("coppermind_store.dropbox.time.monotonic", lambda: clock[0])
    dropbox = store.context.dropbox
    (root / "odd.xyz").write_bytes(b"first")
    await dropbox.scan()
    await settle(dropbox, clock)
    assert (root / "Failed" / "odd.xyz").read_bytes() == b"first"
    assert (root / "Failed" / "odd.xyz.reason.txt").exists()
    (root / "odd.xyz").write_bytes(b"second")
    await dropbox.scan()
    await settle(dropbox, clock)
    assert (root / "Failed" / "odd.xyz").read_bytes() == b"first"
    moved = [p for p in (root / "Failed").iterdir() if p.is_dir()]
    assert len(moved) == 1
    assert (moved[0] / "odd.xyz").read_bytes() == b"second"
    assert (moved[0] / "odd.xyz.reason.txt").exists()


async def test_failed_reason_name_collision_preserves_existing_file(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    root = store.notes_root / "Dropbox"
    failed = root / "Failed"
    failed.mkdir(parents=True)
    existing = failed / "report.bin.reason.txt"
    existing.write_bytes(b"previously failed original")
    (root / "report.bin").write_bytes(b"new unsupported file")
    clock = [0.0]
    monkeypatch.setattr("coppermind_store.dropbox.time.monotonic", lambda: clock[0])
    await store.context.dropbox.scan()
    await settle(store.context.dropbox, clock)
    assert existing.read_bytes() == b"previously failed original"
    folders = [path for path in failed.iterdir() if path.is_dir()]
    assert len(folders) == 1
    assert (folders[0] / "report.bin").read_bytes() == b"new unsupported file"
    assert (folders[0] / "report.bin.reason.txt").is_file()


async def test_a_kind_sidecar_is_consumed_with_its_file(tmp_path, monkeypatch):
    from coppermind.store_protocol import CreatedArtifact

    store = local_store(tmp_path)
    root = store.notes_root / "Dropbox"
    root.mkdir()
    (root / "deck.bin").write_bytes(b"deck")
    (root / "deck.bin.kind").write_text("artifact")
    (root / "bad.xyz").write_bytes(b"bad")
    (root / "bad.xyz.kind").write_text("nonsense")
    created = []

    async def create_artifact(request):
        created.append(request)
        return CreatedArtifact(
            id="01K4Q8Z2A0P1Q2R3S4T5U6V7W8",
            revision=1,
            created=True,
            path="Artifacts/deck.bin",
            title="Deck",
        )

    store.create_artifact = create_artifact
    clock = [0.0]
    monkeypatch.setattr("coppermind_store.dropbox.time.monotonic", lambda: clock[0])
    dropbox = store.context.dropbox
    await dropbox.scan()
    await settle(dropbox, clock)
    assert created
    assert not (root / "deck.bin").exists() and not (root / "deck.bin.kind").exists()
    assert not (root / "bad.xyz").exists() and not (root / "bad.xyz.kind").exists()
    assert (root / "Failed" / "bad.xyz").read_bytes() == b"bad"
    assert (root / "Failed" / "bad.xyz.kind").read_text() == "nonsense"
    assert not (root / "Failed" / "deck.bin").exists()
    assert not (root / "Failed" / "deck.bin.kind").exists()


async def test_files_left_in_dropbox_before_disposal_existed_are_cleared(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    root = store.notes_root / "Dropbox"
    root.mkdir()
    path = root / "left.xyz"
    path.write_bytes(b"left")
    clock = [0.0]
    monkeypatch.setattr("coppermind_store.dropbox.time.monotonic", lambda: clock[0])
    dropbox = store.context.dropbox
    records = dropbox.records()
    info = path.lstat()
    records["observations"]["left.xyz"] = [info.st_mtime_ns, info.st_size, 0, -1]
    dropbox.save(records)
    await dropbox.scan()
    assert any(i["state"] == "stabilizing" for i in dropbox.snapshot()["items"])
    await settle(dropbox, clock)
    assert not path.exists()
    assert (root / "Failed" / "left.xyz").read_bytes() == b"left"


async def test_an_idle_scan_does_not_rewrite_the_records(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    root = store.notes_root / "Dropbox"
    root.mkdir()
    dropbox = store.context.dropbox
    writes = []
    original = dropbox.save

    def counting_save(records):
        writes.append(1)
        original(records)

    monkeypatch.setattr(dropbox, "save", counting_save)
    await dropbox.scan()
    await dropbox.scan()
    await dropbox.process()
    await dropbox.process()
    assert writes == []


async def over_budget_drop(store, root, clock, monkeypatch):
    """Drop a second event while the calendar budget is full, so the import fails.

    Returns the dropped bytes and its Dropbox item. The budget is lifted again
    before returning, so retrying the failed receipt can succeed.
    """
    archive = store.context.archive
    monkeypatch.setattr(archive, "limits", lambda: LimitSettings(calendar_max_events=1))
    dropbox = store.context.dropbox
    dropbox.submit("one.ics", calendar())
    await finish(store)
    dropped = calendar(event="event-2")
    (root / "two.ics").write_bytes(dropped)
    await dropbox.scan()
    clock[0] = 31
    await dropbox.scan()
    await finish(store)
    await dropbox.scan()
    (item,) = [i for i in dropbox.records()["items"].values() if i["filename"] == "two.ics"]
    monkeypatch.setattr(archive, "limits", LimitSettings)
    return dropped, item


async def test_a_failed_drop_that_later_succeeds_leaves_failed(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    root = store.notes_root / "Dropbox"
    root.mkdir()
    clock = [0.0]
    monkeypatch.setattr("coppermind_store.dropbox.time.monotonic", lambda: clock[0])
    dropbox = store.context.dropbox
    dropped, stale = await over_budget_drop(store, root, clock, monkeypatch)
    assert stale["state"] == "failed"
    assert not (root / "two.ics").exists()
    assert (root / "Failed" / "two.ics").read_bytes() == dropped
    assert (root / "Failed" / "two.ics.reason.txt").exists()
    failed = [r for r in store.context.archive.receipts() if r.state == "failed"]
    assert store.context.archive.process(failed[0].id).state == "succeeded"
    await dropbox.process()
    assert dropbox.records()["items"][stale["id"]]["state"] == "succeeded"
    await dropbox.scan()
    assert list((root / "Failed").iterdir()) == []
    assert "failed_path" not in dropbox.records()["items"][stale["id"]]


async def test_resubmitting_a_failed_drop_keeps_its_failed_copy_reference(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    root = store.notes_root / "Dropbox"
    root.mkdir()
    clock = [0.0]
    monkeypatch.setattr("coppermind_store.dropbox.time.monotonic", lambda: clock[0])
    dropbox = store.context.dropbox
    dropped, stale = await over_budget_drop(store, root, clock, monkeypatch)
    failed_path = stale["failed_path"]
    resubmitted = dropbox.submit("two.ics", dropped)
    assert resubmitted["failed_path"] == failed_path
    assert dropbox.records()["items"][stale["id"]]["failed_path"] == failed_path


async def test_a_changed_failed_copy_is_kept_when_the_drop_later_succeeds(tmp_path, monkeypatch):
    store = local_store(tmp_path)
    root = store.notes_root / "Dropbox"
    root.mkdir()
    clock = [0.0]
    monkeypatch.setattr("coppermind_store.dropbox.time.monotonic", lambda: clock[0])
    dropbox = store.context.dropbox
    await over_budget_drop(store, root, clock, monkeypatch)
    (root / "Failed" / "two.ics").write_bytes(b"edited by hand")
    failed = [r for r in store.context.archive.receipts() if r.state == "failed"]
    store.context.archive.process(failed[0].id)
    await dropbox.process()
    await dropbox.scan()
    assert (root / "Failed" / "two.ics").read_bytes() == b"edited by hand"
    assert "differs" in (root / "Failed" / "two.ics.reason.txt").read_text()


OLD_CONFLICT = (
    'Event "Planning" (2026-10-16, UID event-1, entry two.ics) conflicts with stored event '
    '"Planning". The revisions match but the contents differ. Correct the event in its '
    "source calendar and export it again, or dismiss this receipt."
)


@pytest.mark.parametrize("dismissed", [False, True])
async def test_moving_a_stale_failed_file_back_retries_it_with_current_code(
    tmp_path, monkeypatch, dismissed
):
    store = local_store(tmp_path)
    root = store.notes_root / "Dropbox"
    root.mkdir()
    clock = [0.0]
    monkeypatch.setattr("coppermind_store.dropbox.time.monotonic", lambda: clock[0])
    archive, dropbox = store.context.archive, store.context.dropbox
    dropbox.submit("one.ics", calendar())
    await finish(store)
    event_2 = calendar(event="event-2").split(b"BEGIN:VEVENT\r\n", 1)[1]
    other = calendar().replace(b"Planning", b"Other").replace(b"END:VCALENDAR\r\n", b"")
    dropped = other + b"BEGIN:VEVENT\r\n" + event_2
    # The file as an older version left it: failed, non-retryable, old wording, in Failed.
    receipt = archive.submit("calendar", "two.ics", dropped, export_filename="two.ics")
    receipt.state, receipt.error, receipt.retryable = "failed", OLD_CONFLICT, False
    receipt.dismissed = dismissed
    archive._save_receipt(receipt)
    records = dropbox.records()
    records["items"][receipt.content_hash] = {
        "id": receipt.content_hash,
        "filename": "two.ics",
        "state": "failed",
        "parts": [{"import_id": receipt.id}],
        "error": OLD_CONFLICT,
        "received_at": receipt.received_at,
        "kind": {},
        "original_path": "two.ics",
        "failed_path": "Failed/two.ics",
    }
    dropbox.save(records)
    (root / "Failed").mkdir()
    (root / "Failed" / "two.ics").write_bytes(dropped)
    # The owner moves it back to Dropbox unchanged.
    (root / "two.ics").write_bytes(dropped)
    await dropbox.scan()
    clock[0] = 31
    await dropbox.scan()
    await finish(store)
    await dropbox.scan()
    (item,) = [i for i in dropbox.records()["items"].values() if i["filename"] == "two.ics"]
    assert item["state"] == "succeeded" and item["error"] == ""
    (current,) = [r for r in archive.receipts() if r.content_hash == receipt.content_hash]
    assert current.state == "succeeded" and OLD_CONFLICT not in current.error
    assert current.kept_count == 1
    assert not (root / "two.ics").exists()
    assert list((root / "Failed").iterdir()) == []
