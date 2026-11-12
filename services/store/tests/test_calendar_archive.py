"""Import receipts survive restart and prevent partial calendar promotion."""

import pytest
from coppermind_store import calendar_mirror as mirror
from coppermind_store.calendar_archive import CalendarArchive, ImportReceipt
from coppermind_store.calendar_import import (
    CALENDAR_ID,
    CalendarImportError,
    EventProvenance,
    parse_export,
)

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


OLD_WORDING = (
    'Event "Planning" (2026-10-14, UID a, entry old.ics) conflicts with stored event '
    '"Planning". The revisions match but the contents differ. Correct the event in its '
    "source calendar and export it again, or dismiss this receipt."
)


def stale_failed_receipt(archive, payload, *, dismissed=False):
    """A receipt an older version stored as failed, non-retryable, in its old wording."""
    receipt = archive.submit("work", "old.ics", payload)
    receipt.state, receipt.error = "failed", OLD_WORDING
    receipt.retryable, receipt.dismissed = False, dismissed
    archive._save_receipt(receipt)
    return receipt


@pytest.mark.parametrize("dismissed", [False, True])
def test_redropping_a_stale_failed_receipt_reprocesses_it_with_current_code(tmp_path, dismissed):
    archive = CalendarArchive(tmp_path)
    archive.process(archive.submit("work", "base.ics", ics(event("a"), event("b"))).id)
    payload = ics(event("a", title="Changed"), event("b"), event("c"))
    stale = stale_failed_receipt(archive, payload, dismissed=dismissed)

    again = archive.submit("work", "old.ics", payload)
    assert again.id == stale.id
    assert (again.state, again.error, again.dismissed, again.retryable) == (
        "pending",
        "",
        False,
        False,
    )

    done = archive.process(again.id)
    assert done.state == "succeeded" and done.error == ""
    assert done.kept_count == 1 and done.event_count >= 1
    assert "Changed" not in {e.title for e in archive.catalog().events}
    assert len(archive.catalog().events) == 3
    assert len(archive.receipts()) == 2


def test_redropping_a_succeeded_receipt_is_already_imported(tmp_path):
    archive = CalendarArchive(tmp_path)
    payload = ics(event())
    first = archive.process(archive.submit("work", "one.ics", payload).id)
    again = archive.submit("work", "one.ics", payload)
    assert again.state == "succeeded" and again.imported_at == first.imported_at


def test_redropping_unparseable_bytes_fails_again_in_current_wording(tmp_path):
    archive = CalendarArchive(tmp_path)
    receipt = archive.submit("work", "bad.ics", b"not a calendar")
    archive.process(receipt.id)
    stale = archive.submit("work", "bad.ics", b"not a calendar")
    stale.state, stale.error = "failed", "wording from an older version"
    archive._save_receipt(stale)
    again = archive.submit("work", "bad.ics", b"not a calendar")
    assert again.state == "pending" and again.error == ""
    failed = archive.process(again.id)
    assert failed.state == "failed" and "older version" not in failed.error


def test_choosing_the_other_files_version_replaces_the_stored_one_and_settles_the_disagreement(
    tmp_path,
):
    archive = CalendarArchive(tmp_path)
    first = archive.process(archive.submit("work", "a.ics", ics(event("a"))).id)
    second = archive.process(archive.submit("work", "b.ics", ics(event("a", title="Changed"))).id)
    (problem,) = second.conflicts
    (incoming,) = parse_export(CALENDAR_ID, "b.ics", ics(event("a", title="Changed")))
    incoming.provenance = [
        EventProvenance(
            source_id=second.id,
            filename="b.ics",
            entry=incoming.source_entry,
            imported_at=second.received_at,
        )
    ]
    assert archive.catalog().events[problem.event_id].title == "Planning"

    archive.choose_incoming(problem.event_id, problem.version, incoming)

    catalog = archive.catalog()
    chosen = catalog.events[problem.event_id]
    assert chosen.title == "Changed"
    assert {p.source_id for p in chosen.provenance} == {first.id, second.id}
    assert catalog.conflicts == []
    with archive.engine.connect() as connection:
        (settled,) = mirror.get_conflicts(connection)
    assert settled.acknowledged is True
    with pytest.raises(CalendarImportError):
        archive.choose_incoming(problem.event_id, "not-that-version", incoming)
