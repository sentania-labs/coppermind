"""Import receipts survive restart and prevent partial calendar promotion."""

import pytest
from coppermind_store.calendar_archive import CalendarArchive, ImportReceipt

from services.store.tests.test_calendar_import import event, ics


def test_originals_receipts_contacts_and_replay_survive_restart(tmp_path):
    archive = CalendarArchive(tmp_path)
    payload = ics(event())
    receipt = archive.submit("work", "one.ics", payload)
    assert receipt.state == "pending"
    assert (archive.root / "imports" / receipt.id / "original").read_bytes() == payload
    result = archive.process(receipt.id)
    assert result.state == "succeeded"
    restarted = CalendarArchive(tmp_path)
    assert restarted.submit("work", "renamed.ics", payload).id == receipt.id
    assert restarted.process(receipt.id).state == "succeeded"
    catalog = restarted.catalog()
    assert catalog.imports == [receipt.id]
    assert len(catalog.events) == 1
    assert len(catalog.contacts()) == 1
    assert catalog.contacts()[0].evidence[0].role == "invitee"


def test_invalid_export_keeps_previous_catalog_and_archives_failure(tmp_path):
    archive = CalendarArchive(tmp_path)
    first = archive.submit("work", "one.ics", ics(event()))
    archive.process(first.id)
    before = (archive.root / "catalog.json").read_bytes()
    invalid = archive.submit("work", "bad.ics", b"not a calendar")
    failed = archive.process(invalid.id)
    assert failed.state == "failed"
    assert failed.error
    assert (archive.root / "catalog.json").read_bytes() == before
    assert (archive.root / "imports" / failed.id / "original").read_bytes() == b"not a calendar"


def test_crash_after_promotion_finishes_receipt_without_reapplying(tmp_path, monkeypatch):
    archive = CalendarArchive(tmp_path)
    receipt = archive.submit("work", "one.ics", ics(event()))
    original_save = archive._save_receipt

    def fail_final_save(record):
        if record.state == "succeeded":
            raise OSError("simulated interruption after catalog promotion")
        original_save(record)

    monkeypatch.setattr(archive, "_save_receipt", fail_final_save)
    with pytest.raises(OSError):
        archive.process(receipt.id)
    restarted = CalendarArchive(tmp_path)
    completed = restarted.process(receipt.id)
    assert completed.state == "succeeded"
    assert completed.event_count == 1
    assert restarted.catalog().imports == [receipt.id]
    stored = ImportReceipt.model_validate_json(
        (archive.root / "imports" / receipt.id / "receipt.json").read_bytes()
    )
    assert stored.state == "succeeded"


def test_original_integrity_is_checked_before_import(tmp_path):
    archive = CalendarArchive(tmp_path)
    receipt = archive.submit("work", "one.ics", ics(event()))
    (archive.root / "imports" / receipt.id / "original").write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity"):
        archive.process(receipt.id)
    assert archive.catalog().events == {}


def test_cancellation_without_invitees_does_not_forget_contact_corpus(tmp_path):
    archive = CalendarArchive(tmp_path)
    first = archive.submit("work", "one.ics", ics(event()))
    archive.process(first.id)
    contact = archive.catalog().contacts()[0]
    cancellation = archive.submit(
        "work",
        "cancel.ics",
        ics(
            "UID:meeting-1\r\nSEQUENCE:2",
            method="CANCEL",
        ),
    )
    archive.process(cancellation.id)
    preserved = archive.catalog().contacts()[0]
    assert preserved.id == contact.id
    assert preserved.evidence[0].source_import == first.id
