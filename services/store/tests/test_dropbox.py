"""Exports merge automatically; transient receipts never own event history."""

import asyncio
import hashlib
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from coppermind_store import calendar_archive
from coppermind_store import calendar_mirror as mirror
from coppermind_store.calendar_archive import ImportReceipt
from coppermind_store.calendar_import import CalendarImportError, parse_export
from coppermind_store.context_service import ContextService
from coppermind_store.control import ControlState
from coppermind_store.notes import LocalStore

from coppermind.context_config import CalendarSource
from coppermind.settings import Wiring
from coppermind.store_protocol import MetadataUnavailable, NotFound
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


async def test_conflicting_source_fails_without_overwriting(tmp_path):
    store = local_store(tmp_path)
    store.context.dropbox.submit("one.ics", calendar())
    await finish(store)
    conflict = store.context.dropbox.submit("two.ics", calendar().replace(b"Planning", b"Conflict"))
    await finish(store)
    receipt = store.context.dropbox.records()["items"][conflict["id"]]
    assert receipt["state"] == "failed"
    assert "conflicts" in receipt["error"]
    assert next(iter(store.context.archive.catalog().events.values())).title == "Planning"


async def test_dismissing_failed_receipt_acknowledges_import_failure(tmp_path):
    store = local_store(tmp_path)
    dropbox = store.context.dropbox
    dropbox.submit("one.ics", calendar())
    await finish(store)
    conflict = dropbox.submit("two.ics", calendar().replace(b"Planning", b"Conflict"))
    await finish(store)
    failed = [r for r in store.context.archive.receipts() if r.state == "failed"]
    assert [r.dismissed for r in failed] == [False]
    await dropbox.dismiss({"id": conflict["id"]})
    assert [r.dismissed for r in store.context.archive.receipts() if r.state == "failed"] == [True]
    retried = store.context.archive.process(failed[0].id)
    assert retried.state == "failed"
    assert retried.dismissed is False
    store.context.archive.dismiss(failed[0].id)
    redrop = dropbox.submit("again.ics", calendar().replace(b"Planning", b"Conflict"))
    await finish(store)
    assert dropbox.records()["items"][redrop["id"]]["state"] == "failed"
    assert [r.dismissed for r in store.context.archive.receipts() if r.state == "failed"] == [False]


async def test_retried_receipt_that_succeeds_completes_failed_dropbox_item(tmp_path):
    store = local_store(tmp_path)
    dropbox = store.context.dropbox
    dropbox.submit("one.ics", calendar())
    await finish(store)
    stale_bytes = calendar().replace(b"Planning", b"Conflict")
    stale = dropbox.submit("two.ics", stale_bytes)
    await finish(store)
    assert dropbox.records()["items"][stale["id"]]["state"] == "failed"
    newer = calendar().replace(b"Planning", b"Newer").replace(b"SUMMARY", b"SEQUENCE:1\r\nSUMMARY")
    dropbox.submit("three.ics", newer)
    await finish(store)
    failed = [r for r in store.context.archive.receipts() if r.state == "failed"]
    assert store.context.archive.process(failed[0].id).state == "succeeded"
    await dropbox.process()
    item = dropbox.records()["items"][stale["id"]]
    assert item["state"] == "succeeded"
    assert item["error"] == ""
    assert dropbox.submit("again.ics", stale_bytes)["state"] == "already_imported"


async def test_import_history_dismisses_direct_and_dropbox_failures(tmp_path):
    store = local_store(tmp_path)
    context = store.context
    context.archive.process(context.archive.submit("calendar", "first.ics", calendar()).id)
    direct = context.archive.submit(
        "calendar", "direct.ics", calendar().replace(b"Planning", b"Direct")
    )
    assert context.archive.process(direct.id).state == "failed"
    dropped = context.dropbox.submit("dropped.ics", calendar().replace(b"Planning", b"Dropped"))
    await finish(store)
    assert context.dropbox.records()["items"][dropped["id"]]["state"] == "failed"
    failed = [r for r in context.archive.receipts() if r.state == "failed"]
    assert len(failed) == 2
    for receipt in failed:
        assert await context.dismiss_import({"id": receipt.id}) == {"dismissed": receipt.id}
        with pytest.raises(ValueError, match="dismissed"):
            await context.retry_import({"id": receipt.id})
    assert all(r.dismissed for r in context.archive.receipts() if r.state == "failed")
    assert dropped["id"] not in context.dropbox.records()["items"]
    snapshot = await context.snapshot()
    assert {r["filename"] for r in snapshot["calendar"]["imports"] if r["dismissed"]} == {
        "direct.ics",
        "dropped.ics",
    }
    assert len(context.archive.receipts()) == 3
    succeeded = next(r for r in context.archive.receipts() if r.state == "succeeded")
    with pytest.raises(ValueError, match="Only failed"):
        await context.dismiss_import({"id": succeeded.id})
    with pytest.raises(ValueError, match="Unknown"):
        await context.dismiss_import({"id": "0" * 64})


async def test_dismissing_failed_import_before_reconcile_clears_importing_item(tmp_path):
    store = local_store(tmp_path)
    context = store.context
    context.archive.process(context.archive.submit("calendar", "first.ics", calendar()).id)
    dropped = context.dropbox.submit("late.ics", calendar().replace(b"Planning", b"Late"))
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
        path.unlink()
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
    archive.process(archive.submit("calendar", "direct.ics", bad).id)
    store.context.dropbox.submit("redrop.zip", zip_bytes({"Work/Meeting.ics": bad}))
    await finish(store)
    failures = [r for r in archive.receipts() if r.state == "failed"]
    assert len(failures) == 2
    zipped = next(r for r in failures if r.filename.endswith("zip"))
    for detail in (
        "Wrong title",
        "Planning",
        "event-1",
        "Work/Meeting.ics",
        "Incoming revision",
        "stored revision",
    ):
        assert detail in zipped.error
    for receipt in failures:
        await store.context.dismiss_import({"id": receipt.id})
        with pytest.raises(ValueError, match="dismissed"):
            await store.context.retry_import({"id": receipt.id})
    assert store.context.dropbox.snapshot()["items"] == []
    assert all(r.dismissed for r in archive.receipts() if r.state == "failed")
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


async def test_redrop_retries_a_failed_non_conflict_import_and_keeps_conflicts_failed(
    tmp_path, monkeypatch
):
    store = local_store(tmp_path)
    archive = store.context.archive
    real = calendar_archive.parse_bounded

    def slow(*args, **kwargs):
        raise CalendarImportError("calendar processing exceeded its time limit")

    monkeypatch.setattr("coppermind_store.calendar_archive.parse_bounded", slow)
    first = store.context.dropbox.submit("big.ics", calendar())
    await finish(store)
    failed = store.context.dropbox.records()["items"][first["id"]]
    assert failed["state"] == "failed"
    assert failed["error"].endswith("time limit. Drop the export again to retry.")
    assert not archive.receipts()[0].conflict

    monkeypatch.setattr("coppermind_store.calendar_archive.parse_bounded", real)
    again = store.context.dropbox.submit("big.ics", calendar())
    assert again["state"] == "pending"
    await finish(store)
    item = store.context.dropbox.records()["items"][first["id"]]
    assert item["state"] == "succeeded" and item["error"] == ""
    assert [r.state for r in archive.receipts()] == ["succeeded"]
    assert len(archive.catalog().events) == 1

    conflicting = calendar().replace(b"Planning", b"Wrong title")
    store.context.dropbox.submit("conflict.ics", conflicting)
    await finish(store)
    receipt = next(r for r in archive.receipts() if r.filename == "conflict.ics")
    assert receipt.state == "failed" and receipt.conflict
    assert "Drop the export again" not in receipt.error
    store.context.dropbox.submit("conflict.ics", conflicting)
    await finish(store)
    assert next(r for r in archive.receipts() if r.id == receipt.id).state == "failed"


def fake_notes(store, notes, monkeypatch, fail=()):
    patched = []

    async def get_note(note_id):
        if note_id not in notes:
            raise NotFound(note_id)
        return SimpleNamespace(
            id=note_id, frontmatter=dict(notes[note_id]), content_hash="hash-" + note_id
        )

    async def patch_frontmatter(note_id, request, if_match):
        if note_id in fail:
            raise MetadataUnavailable("database down")
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
    patched = fake_notes(store, notes, monkeypatch, fail={"n3"})

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
