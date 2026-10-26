"""Actual ICS and ZIP fixtures exercise identity, revisions, and partial exports."""

import io
import zipfile

import pytest
from coppermind_store.calendar_import import (
    MAX_EXPANDED,
    MAX_SERIALIZED,
    MAX_UPLOAD,
    CalendarImportError,
    merge_events,
    parse_export,
)
from icalendar import Calendar


def large_ics(uid: str = "large", n: int = 12000) -> bytes:
    """A synthetic single calendar between 10 and 40 MiB, built in-process, no fixture file."""
    head = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\nMETHOD:PUBLISH\r\n" + (
        f"UID:{uid}\r\n" if uid else ""
    )
    body = "\r\n".join(
        f"BEGIN:VEVENT\r\nUID:meeting-{i}\r\nSEQUENCE:0\r\nSUMMARY:Planning\r\n"
        "DTSTART;TZID=America/Chicago:20261014T090000\r\n"
        "DTEND;TZID=America/Chicago:20261014T100000\r\n"
        "DESCRIPTION:" + "x" * 750 + "\r\nEND:VEVENT"
        for i in range(n)
    )
    return (head + body + "\r\nEND:VCALENDAR\r\n").encode()


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


def test_expanded_ics_over_10_mib_passes_the_expanded_ceiling():
    source = large_ics()
    assert MAX_UPLOAD < len(source) < MAX_EXPANDED
    records = parse_export("work", "calendar.ics", source, MAX_EXPANDED)
    assert len(records) == 12000
    assert records[0].source_entry == "calendar.ics"


def test_zip_over_10_mib_expanding_under_40_mib_passes():
    source = zip_bytes({"calendar.ics": large_ics()})
    assert len(source) < MAX_UPLOAD
    assert len(parse_export("work", "export.zip", source, MAX_EXPANDED)) == 12000


def test_top_of_range_single_calendar_fits_the_serialized_ceiling():
    # 50000 records is the last size the event ceiling allows and the top of the 40 MiB
    # expansion. `to_ical` re-folds long properties, so the handoff bytes are measured
    # against MAX_SERIALIZED rather than the expansion they came from.
    source = large_ics(n=45000)
    assert MAX_UPLOAD < len(source) < MAX_EXPANDED
    records = parse_export("work", "calendar.ics", source, MAX_SERIALIZED)
    assert len(records) == 45000
    assert len(Calendar.from_ical(source).to_ical()) <= MAX_SERIALIZED


def test_original_upload_over_10_mib_is_rejected():
    source = large_ics()
    with pytest.raises(CalendarImportError, match="exceeds 10 MiB"):
        parse_export("work", "calendar.ics", source)


def test_zip_expanding_past_40_mib_is_rejected():
    # Four entries of ~11.9 MiB each: the archive is small, the expansion is ~47 MiB.
    source = zip_bytes({f"{n}.ics": large_ics(str(n)) for n in range(4)})
    with pytest.raises(CalendarImportError, match="40 MiB"):
        parse_export("work", "export.zip", source, MAX_EXPANDED)


def test_archive_entry_and_event_count_protections_still_trigger():
    many = zip_bytes({f"{n}.ics": ics(event(f"e-{n}")) for n in range(101)})
    with pytest.raises(CalendarImportError, match="100 entries"):
        parse_export("work", "many.zip", many)
    oversized = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\nMETHOD:PUBLISH\r\n"
        + "\r\n".join(
            f"BEGIN:VEVENT\r\nUID:e-{n}\r\nDTSTART:20261014T090000Z\r\nEND:VEVENT"
            for n in range(50001)
        )
        + "\r\nEND:VCALENDAR\r\n"
    ).encode()
    assert len(oversized) < MAX_UPLOAD
    with pytest.raises(CalendarImportError, match="50000 event records"):
        parse_export("work", "many.ics", oversized)


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


def test_parse_output_sends_each_timezone_definition_once():
    from coppermind_store.calendar_worker import parse_bounded

    zones = "".join(
        f"BEGIN:VTIMEZONE\r\nTZID:Zone/{i}\r\nBEGIN:STANDARD\r\nDTSTART:19701101T020000\r\n"
        f"TZOFFSETFROM:-0{i}00\r\nTZOFFSETTO:-0{i}00\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n"
        for i in range(1, 4)
    )
    source = ics(event("one"), event("two")).replace(
        b"BEGIN:VEVENT", zones.encode() + b"BEGIN:VEVENT", 1
    )
    expected = parse_export("work", "calendar.ics", source)
    records = parse_bounded("work", "calendar.ics", source)
    assert [r.model_dump() for r in records] == [e.model_dump() for e in expected]
    assert len(records[0].timezone_components) == 3
