"""Actual ICS and ZIP fixtures exercise identity, revisions, and partial exports."""

import io
import zipfile

import pytest
from coppermind_store.calendar_import import (
    CalendarImportError,
    merge_events,
    parse_export,
)


def ics(*events: str, method: str = "PUBLISH") -> bytes:
    body = "\r\n".join(f"BEGIN:VEVENT\r\n{event}\r\nEND:VEVENT" for event in events)
    return (
        f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\n"
        f"METHOD:{method}\r\n{body}\r\nEND:VCALENDAR\r\n"
    ).encode()


def event(uid: str = "meeting-1", sequence: int = 0, title: str = "Planning") -> str:
    return (
        f"UID:{uid}\r\nSEQUENCE:{sequence}\r\nSUMMARY:{title}\r\n"
        "DTSTART;TZID=America/Chicago:20261014T090000\r\n"
        "DTEND;TZID=America/Chicago:20261014T100000\r\n"
        "ATTENDEE;CN=Casey;PARTSTAT=ACCEPTED:mailto:Casey@EXAMPLE.COM"
    )


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return output.getvalue()


def test_filename_changes_do_not_change_calendar_event_or_contact_identity():
    first = parse_export("work", "export.ics", ics(event()))[0]
    renamed = parse_export("work", "next.ics", ics(event()))[0]
    other = parse_export("personal", "next.ics", ics(event()))[0]
    assert first.identity == renamed.identity != other.identity
    assert first.start == "2026-10-14T09:00:00-05:00"
    assert first.people[0].role == "invitee"
    assert first.people[0].participation == "ACCEPTED"
    assert first.people[0].contact_id == renamed.people[0].contact_id
    assert (
        first.people[0].contact_id
        == first.people[0].model_copy(update={"address": "mailto:Casey@example.com"}).contact_id
    )
    assert (
        first.people[0].contact_id
        != first.people[0].model_copy(update={"address": "mailto:casey@example.com"}).contact_id
    )


def test_partial_older_and_cancelled_exports():
    initial = parse_export("work", "one.ics", ics(event(), event("other")))
    current = {item.identity: item for item in initial}
    newer = parse_export("work", "two.ics", ics(event(sequence=2, title="New")))
    merged = merge_events(current, newer)
    assert len(merged) == 2
    assert merged[newer[0].identity].title == "New"
    assert merge_events(merged, initial) == merged
    cancelled = parse_export(
        "work", "cancel.ics", ics("UID:meeting-1\r\nSEQUENCE:3", method="CANCEL")
    )
    assert merge_events(merged, cancelled)[newer[0].identity].cancelled


def test_equal_revision_conflict_is_not_last_writer_wins():
    first = parse_export("work", "one.ics", ics(event()))
    conflicting = parse_export("work", "two.ics", ics(event(title="Different")))
    with pytest.raises(CalendarImportError, match="conflicts"):
        merge_events({first[0].identity: first[0]}, conflicting)
    with pytest.raises(CalendarImportError, match="conflicting"):
        parse_export("work", "one.ics", ics(event(), event(title="Different")))


def test_zip_multiple_calendars_and_recurrence_exceptions_are_retained():
    source = zip_bytes(
        {
            "one.ics": ics(event() + "\r\nRRULE:FREQ=WEEKLY;COUNT=3"),
            "nested/two.ics": ics(
                event(sequence=1) + "\r\nRECURRENCE-ID;TZID=America/Chicago:20261021T090000"
            ),
        }
    )
    records = parse_export("work", "export.zip", source)
    assert len(records) == 2
    assert len({item.identity for item in records}) == 2
    assert "RRULE:FREQ=WEEKLY;COUNT=3" in records[0].component


@pytest.mark.parametrize("name", ["../bad.ics", "/bad.ics", "nested.zip", "file.txt"])
def test_zip_rejects_unsafe_or_unsupported_entries(name):
    with pytest.raises(CalendarImportError):
        parse_export("work", "bad.zip", zip_bytes({name: ics(event())}))


def test_entire_export_rejected_when_one_event_is_invalid():
    with pytest.raises(CalendarImportError, match="UID"):
        parse_export("work", "bad.ics", ics(event(), "DTSTART:20261014T090000Z"))


def test_modification_time_compared_by_instant_not_offset_string():
    first = parse_export("work", "one.ics", ics(event() + "\r\nLAST-MODIFIED:20261014T150000Z"))[0]
    assert first.revision[1] == "2026-10-14T15:00:00+00:00"


def test_recurrence_lookup_handles_dst_exception_and_cancellation():
    from datetime import date

    from coppermind_store.calendar_worker import occurrences

    recurring = event() + "\r\nRRULE:FREQ=WEEKLY;COUNT=5"
    records = parse_export("work", "one.ics", ics(recurring))
    result = occurrences(records, date(2026, 11, 4), "America/Chicago")
    assert result[0]["start"] == "2026-11-04T09:00:00-06:00"
    cancelled = parse_export(
        "work",
        "cancel.ics",
        ics(
            "UID:meeting-1\r\nSEQUENCE:1\r\nRECURRENCE-ID;TZID=America/Chicago:20261104T090000",
            method="CANCEL",
        ),
    )
    # Cancellation messages often omit DTSTART. The stored recurrence identity
    # must still suppress the occurrence.
    result = occurrences(
        list(merge_events({item.identity: item for item in records}, cancelled).values()),
        date(2026, 11, 4),
        "America/Chicago",
    )
    assert result == []
