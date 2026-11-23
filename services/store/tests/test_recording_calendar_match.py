"""Recordings shaped like the owner's first week match their calendar events (issue #151)."""

from types import SimpleNamespace

import pytest
from coppermind_store.calendar_archive import CalendarArchive
from coppermind_store.context_service import ContextService
from coppermind_store.recording_writeup import clean_title, resolve_title

from coppermind.context_config import FeatureAssignment
from coppermind.settings import default_settings
from services.store.tests.test_calendar_import import ics

CHICAGO = "TZID=America/Chicago"


def vevent(uid, title, start, end, extra=""):
    return f"UID:{uid}\r\nSEQUENCE:0\r\nSUMMARY:{title}\r\n{start}\r\n{end}\r\n{extra}".rstrip()


def timed(uid, title, day, begin, finish, extra=""):
    return vevent(
        uid,
        title,
        f"DTSTART;{CHICAGO}:{day}T{begin}00",
        f"DTEND;{CHICAGO}:{day}T{finish}00",
        extra,
    )


CALENDAR = [
    timed("customer", "Customer sync", "20261119", "0800", "0900"),
    vevent(
        "weekly",
        "Weekly customer meeting",
        f"DTSTART;{CHICAGO}:20261105T140000",
        f"DTEND;{CHICAGO}:20261105T150000",
        "RRULE:FREQ=WEEKLY;COUNT=10",
    ),
    timed("personal", "Dentist", "20261120", "1030", "1130"),
    vevent("holiday", "Office closed", "DTSTART;VALUE=DATE:20261121", "DTEND;VALUE=DATE:20261122"),
    timed("late", "Evening review", "20261119", "2000", "2100")
    .replace(f"DTSTART;{CHICAGO}:20261119T200000", "DTSTART:20261120T020000Z")
    .replace(f"DTEND;{CHICAGO}:20261119T210000", "DTEND:20261120T030000Z"),
    # Two stand-ins for a busy calendar: overlapping holds the transcript cannot tell apart.
    timed("hold-a", "Hold A", "20261124", "0900", "1000"),
    timed("hold-b", "Hold B", "20261124", "0900", "1000"),
]
# Years of unrelated history, like a real export.
CALENDAR += [
    timed(f"old{i}", f"Old {i}", f"2024{i % 12 + 1:02d}15", "0900", "1000") for i in range(300)
]


@pytest.fixture(scope="module")
def context(tmp_path_factory):
    archive = CalendarArchive(tmp_path_factory.mktemp("calendar"))
    receipt = archive.submit("calendar", "export.ics", ics(*CALENDAR))
    assert archive.process(receipt.id).state == "succeeded"
    return SimpleNamespace(
        archive=archive,
        calendar_candidates=None,
        config=lambda: SimpleNamespace(assignments={"enrichment": FeatureAssignment()}),
        store=SimpleNamespace(
            control=SimpleNamespace(
                settings=default_settings,
                schema=lambda: SimpleNamespace(role=lambda name: "date"),
            )
        ),
    )


def job(captured_at, title="Meeting: Recorder title", frontmatter=None):
    return SimpleNamespace(
        id="job",
        captured_at=captured_at,
        created_at="2026-11-25T00:00:00Z",
        frontmatter=frontmatter or {},
        title=title,
    )


async def resolve(context, captured_at, **kwargs):
    context.calendar_candidates = lambda note, d=False: ContextService.calendar_candidates(
        context, note, d
    )
    return await resolve_title(context, job(captured_at, **kwargs), "A greeting.")


@pytest.mark.parametrize(
    ("captured_at", "title"),
    [
        ("2026-11-19T08:01:00-06:00", "Customer sync"),  # a minute late
        ("2026-11-19T14:17:00-06:00", "Weekly customer meeting"),  # joined 17 minutes late
        ("2026-11-26T14:05:00-06:00", "Weekly customer meeting"),  # a later occurrence
        ("2026-11-20T10:35:00-06:00", "Dentist"),  # a personal appointment
        ("2026-11-19T07:55:00-06:00", "Customer sync"),  # a few minutes early
        ("2026-11-20T02:05:00+00:00", "Evening review"),  # sent in UTC, local evening before
        ("2026-11-19T08:01:00", "Customer sync"),  # no offset: read in the lab's zone
    ],
)
async def test_recording_matches_its_event(context, captured_at, title):
    resolved, evidence = await resolve(context, captured_at)
    assert evidence["matched"], evidence
    assert resolved.endswith(" " + title)


@pytest.mark.parametrize(
    ("captured_at", "reason"),
    [
        ("2026-11-21T12:00:00-06:00", "no calendar event overlaps"),  # only an all-day event
        ("2026-11-19T12:00:00-06:00", "no calendar event overlaps"),
        ("2026-11-24T09:10:00-06:00", "2 calendar events overlap"),
    ],
)
async def test_unmatched_recording_says_why(context, captured_at, reason):
    resolved, evidence = await resolve(context, captured_at)
    assert not evidence["matched"]
    assert reason in evidence["reason"]
    assert resolved.endswith(" Recorder title")


async def test_a_failed_lookup_is_named_and_logged(context):
    async def broken(note, descriptions=False):
        raise TimeoutError

    context.calendar_candidates = broken
    resolved, evidence = await resolve_title(
        context, job("2026-11-19T08:01:00-06:00"), "A greeting."
    )
    assert not evidence["matched"]
    assert "lookup failed (TimeoutError)" in evidence["reason"]
    assert resolved == "2026-11-19 Recorder title"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("11-19 Meeting: Customer Growth", "Customer Growth"),
        ("2026-11-19 Customer Growth", "Customer Growth"),
        ("2026-11-19: Customer Growth", "Customer Growth"),
        ("11/19 Customer Growth", "Customer Growth"),
        ("11/19: Customer Growth", "Customer Growth"),
        ("11-19_Customer Growth", "Customer Growth"),
        ("11-19Customer Growth", "Customer Growth"),
        ("2026/11/19 - Customer Growth", "Customer Growth"),
        ("Meeting: 2026-11-19", "2026-11-19"),
        ("11-19 Operations Meeting: Roadmap", "Operations Meeting: Roadmap"),
        ("11-19 Technical Meeting: Roadmap", "Technical Meeting: Roadmap"),
        ("11-19 Meeting:", "Meeting:"),
        ("11-19", "Recording"),
        # A leading number that is not this recording's date stays.
        ("11-20 Meeting: Roadmap", "11-20 Meeting: Roadmap"),
        ("2025-11-19 Roadmap", "2025-11-19 Roadmap"),
        ("3-2 split plan", "3-2 split plan"),
        ("2026 plan", "2026 plan"),
        ("11-195 units", "11-195 units"),
        ("Meeting: Roadmap", "Roadmap"),
        ("meeting:Roadmap", "Roadmap"),
        ("Meetings: Roadmap", "Meetings: Roadmap"),
    ],
)
def test_title_loses_only_its_own_leading_date(title, expected):
    from datetime import date

    assert clean_title(title, date(2026, 11, 19)) == expected


async def test_single_digit_dates(context):
    from datetime import date

    assert clean_title("1-5 Meeting: Standup", date(2026, 1, 5)) == "Standup"
    assert clean_title("01-05 Standup", date(2026, 1, 5)) == "Standup"
    assert clean_title("1/5 Standup", date(2026, 1, 5)) == "Standup"


async def test_unmatched_title_is_not_doubled(context):
    resolved, _ = await resolve(
        context,
        "2026-11-19T12:00:00-06:00",
        title="11-19 Meeting: Customer Growth, AI Governance, and Infrastructure Right-Sizing",
    )
    assert resolved == (
        "2026-11-19 Customer Growth, AI Governance, and Infrastructure Right-Sizing"
    )
