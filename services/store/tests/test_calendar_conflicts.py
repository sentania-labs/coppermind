"""Equal-revision conflicts keep the stored copy and report per event (issue #122)."""

import pytest
from coppermind_store.calendar_import import (
    CalendarEvent,
    CalendarImportError,
    merge_event_into,
    merge_events,
    merge_with_problems,
    parse_export,
)


def ics(*events: str, method: str = "PUBLISH") -> bytes:
    body = "\r\n".join(f"BEGIN:VEVENT\r\n{event}\r\nEND:VEVENT" for event in events)
    return (
        f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\n"
        f"METHOD:{method}\r\n{body}\r\nEND:VCALENDAR\r\n"
    ).encode()


def event(
    uid: str = "meeting-1",
    sequence: int = 0,
    title: str = "Planning",
    description: str = "x" * 8,
) -> str:
    return (
        f"UID:{uid}\r\nSEQUENCE:{sequence}\r\nSUMMARY:{title}\r\n"
        "DTSTART;TZID=America/Chicago:20261014T090000\r\n"
        "DTEND;TZID=America/Chicago:20261014T100000\r\n"
        f"DESCRIPTION:{description}\r\n"
        "LOCATION:Room 2\r\n"
        "ATTENDEE;CN=Casey;PARTSTAT=ACCEPTED:mailto:Casey@EXAMPLE.COM"
    )


def test_one_conflicting_event_costs_only_itself():
    stored = parse_export("calendar", "one.ics", ics(event("a"), event("b"), event("c")))
    current = {item.identity: item for item in stored}
    incoming = parse_export(
        "calendar",
        "two.ics",
        ics(event("a", title="Changed"), event("b"), event("c", sequence=1, title="Later")),
    )
    merged, problems = merge_with_problems(current, incoming)

    assert len(merged) == 3
    assert len(problems) == 1
    # The conflicting event keeps the stored copy; the newer revision still wins.
    assert merged[stored[0].identity].title == "Planning"
    assert merged[stored[2].identity].title == "Later"
    assert merged[stored[1].identity] == stored[1]
    assert problems[0].event_id == stored[0].identity
    assert "title" in problems[0].reason
    assert "stored version was kept" in problems[0].reason


def test_every_conflict_in_an_export_is_reported_in_one_pass():
    stored = parse_export("calendar", "one.ics", ics(event("a"), event("b"), event("c")))
    current = {item.identity: item for item in stored}
    incoming = parse_export(
        "calendar",
        "two.ics",
        ics(
            event("a", title="Changed"),
            event("b", description="y" * 8),
            event("c", title="Also changed"),
        ),
    )
    merged, problems = merge_with_problems(current, incoming)

    assert len(problems) == 3
    assert {problem.event_id for problem in problems} == {item.identity for item in stored}
    assert all("title" in problem.reason for problem in problems[:1])
    reasons = {problem.event_id: problem.reason for problem in problems}
    assert "description" in reasons[stored[1].identity]
    assert all(problem.version for problem in problems)


def test_an_acknowledged_conflict_stays_quiet_on_reimport():
    stored = parse_export("calendar", "one.ics", ics(event()))
    current = {item.identity: item for item in stored}
    incoming = parse_export("calendar", "two.ics", ics(event(title="Changed")))
    first, problems = merge_with_problems(current, incoming)
    assert len(problems) == 1

    # The durable mirror keeps the stored copy, so the acknowledgement matches it.
    retained = {item.identity: item for item in stored}
    _, again = merge_with_problems(retained, incoming)
    assert again == problems
    assert first[stored[0].identity] == stored[0]


def test_a_difference_only_in_ignored_properties_is_not_a_conflict():
    stored = parse_export(
        "calendar", "one.ics", ics(event() + "\r\nLAST-MODIFIED:20261014T150000Z")
    )
    current = {item.identity: item for item in stored}
    incoming = parse_export(
        "calendar",
        "two.ics",
        ics(
            event(title="Planning").replace("PARTSTAT=ACCEPTED", "PARTSTAT=TENTATIVE")
            + "\r\nLAST-MODIFIED:20261015T150000Z"
        ),
    )

    assert incoming[0].semantic_hash == stored[0].semantic_hash
    merged, problems = merge_with_problems(current, incoming)
    assert problems == []
    # Equal hash and equal revision, so the incoming record simply replaces the
    # stored one and only its shown fields move.
    assert merged[stored[0].identity] == incoming[0]
    assert merged[stored[0].identity].people[0].participation == "TENTATIVE"


def test_a_genuinely_newer_revision_still_updates():
    stored = parse_export("calendar", "one.ics", ics(event(sequence=1)))
    current = {item.identity: item for item in stored}
    incoming = parse_export(
        "calendar", "two.ics", ics(event(sequence=2, title="Changed") + "\r\nLOCATION:Room 9")
    )
    merged, problems = merge_with_problems(current, incoming)

    assert problems == []
    assert merged[stored[0].identity].title == "Changed"
    assert merged[stored[0].identity].sequence == 2


def test_a_hash_change_reimporting_identical_bytes_is_neither_conflict_nor_update():
    source = ics(event(), event("b", sequence=4))
    stored = parse_export("calendar", "one.ics", source)
    current = {item.identity: item for item in stored}
    again = parse_export("calendar", "one.ics", source)

    merged, problems = merge_with_problems(current, again)
    assert problems == []
    assert merged == current


def test_differences_name_every_changed_part_in_plain_words():
    stored = parse_export("calendar", "one.ics", ics(event()))[0]
    incoming = parse_export(
        "calendar",
        "two.ics",
        ics(
            "UID:meeting-1\r\nSEQUENCE:0\r\nSUMMARY:Changed\r\n"
            "DTSTART;TZID=America/Chicago:20261014T110000\r\n"
            "DTEND;TZID=America/Chicago:20261014T120000\r\n"
            "DESCRIPTION:yyyyyyyy\r\n"
            "LOCATION:Room 9\r\n"
            "ATTENDEE;CN=Casey;PARTSTAT=ACCEPTED:mailto:casey@example.com"
        ),
    )[0]

    merged, problems = merge_with_problems({stored.identity: stored}, [incoming])
    reason = problems[0].reason
    for part in ("title", "description", "location"):
        assert part in reason
    assert "DTSTART" not in reason
    assert "SUMMARY" not in reason
    # The retained record is the stored copy, not the incoming one.
    assert merged[stored.identity] == stored


def test_merge_events_keeps_the_same_shape_as_before():
    stored = parse_export("calendar", "one.ics", ics(event()))
    current = {item.identity: item for item in stored}
    incoming = parse_export("calendar", "two.ics", ics(event(sequence=1)))

    assert merge_events(current, incoming)[stored[0].identity].sequence == 1
    merged: dict[str, CalendarEvent] = dict(current)
    merge_event_into(merged, incoming[0])
    assert merged == merge_events(current, incoming)


def test_two_conflicting_copies_inside_one_export_are_rejected():
    data = ics(event(), event(title="Different"))
    with pytest.raises(CalendarImportError, match="twice with the same revision"):
        parse_export("calendar", "one.ics", data)
