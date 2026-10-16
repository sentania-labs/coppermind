"""Calendar-source identity is separate from event identity and export filenames."""

import base64
import io
import zipfile

import pytest
from coppermind_store.calendar_worker import isolated_calendar


def calendar(uid="", event="event-1", name="Family"):
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\n"
        + (f"UID:{uid}\r\n" if uid else "")
        + f"X-WR-CALNAME:{name}\r\nBEGIN:VEVENT\r\nUID:{event}\r\n"
        "DTSTART:20261016T150000Z\r\nSUMMARY:Planning\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


def identify(data, filename="calendar.ics"):
    return isolated_calendar(
        {"action": "identify", "filename": filename, "data": base64.b64encode(data).decode()}
    )


def test_uses_calendar_uid_not_event_uid_or_name():
    assert identify(calendar("family"))[0]["uid"] == "family"
    assert identify(calendar())[0]["uid"] == ""
    assert identify(calendar("work", name="Family"))[0]["uid"] == "work"


def test_zip_keeps_physical_calendars_separate_and_refuses_mixed_content():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("work.ics", calendar("work"))
        z.writestr("family.ics", calendar("family"))
    assert {p["uid"] for p in identify(archive.getvalue(), "export.zip")} == {"work", "family"}
    with zipfile.ZipFile(archive, "a") as z:
        z.writestr("slides.pdf", b"not a calendar")
    with pytest.raises(ValueError, match="only ICS"):
        identify(archive.getvalue(), "export.zip")
