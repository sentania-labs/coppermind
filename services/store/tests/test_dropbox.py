"""Calendar-source identity is separate from event identity and export filenames."""

import base64
import io
import zipfile
from typing import Any, cast

import pytest
from coppermind_store.calendar_worker import isolated_calendar
from coppermind_store.control import ControlState
from coppermind_store.notes import LocalStore

from coppermind.settings import Wiring
from services.store.tests.test_calendar_import import large_ics, zip_bytes


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


def test_calendars_in_one_zip_are_validated_separately():
    # The same meeting in two calendars of one export differs only in its timezone context,
    # which is not a conflict: each calendar is imported under its own identity.
    zone = (
        "BEGIN:VTIMEZONE\r\nTZID:Zone/One\r\nBEGIN:STANDARD\r\nDTSTART:19701101T020000\r\n"
        "TZOFFSETFROM:-0100\r\nTZOFFSETTO:-0100\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n"
    )
    first = calendar("work").replace(b"BEGIN:VEVENT", zone.encode() + b"BEGIN:VEVENT")
    source = zip_bytes({"work.ics": first, "family.ics": calendar("family")})
    assert {p["uid"] for p in identify(source, "export.zip")} == {"work", "family"}


def test_identify_hands_off_the_calendar_as_exported():
    cancelled = calendar("work").replace(b"VERSION:2.0", b"VERSION:2.0\r\nMETHOD:CANCEL")
    handoff = base64.b64decode(identify(cancelled)[0]["data"])
    assert b"STATUS:CANCELLED" not in handoff


def local_store(tmp_path):
    wiring = Wiring(data_dir=tmp_path / "data")
    control = ControlState(wiring.state_dir)
    control.ensure_defaults()
    wiring.notes_dir.mkdir(parents=True, exist_ok=True)
    return LocalStore(wiring.notes_dir, control, cast(Any, object()), wiring.sources_dir)


async def test_zip_with_an_ics_over_10_mib_imports_when_identity_is_available(tmp_path):
    store = local_store(tmp_path)
    source = zip_bytes({"calendar.ics": large_ics("large")})
    item = store.context.dropbox.submit("export.zip", source)
    await store.context.dropbox.process()
    record = store.context.dropbox.records()["items"][item["id"]]
    # One process pass identifies the part and hands it to the archive, which is why the
    # receipt is pending while the item sits at `importing`.
    assert record["state"] == "importing"
    assert record["parts"][0]["uid"] == "large"
    for receipt in store.context.archive.receipts():
        assert store.context.archive.process(receipt.id).event_count == 12000
    await store.context.dropbox.process()
    assert store.context.dropbox.records()["items"][item["id"]]["state"] == "succeeded"
    assert len(store.context.archive.catalog().events) == 12000


async def test_zip_with_an_ics_over_10_mib_imports_after_an_explicit_identity_choice(tmp_path):
    store = local_store(tmp_path)
    # No calendar source is configured, so an unnamed calendar needs an explicit choice.
    unnamed = large_ics().replace(b"UID:large\r\n", b"")
    item = store.context.dropbox.submit("export.zip", zip_bytes({"calendar.ics": unnamed}))
    await store.context.dropbox.process()
    record = store.context.dropbox.records()["items"][item["id"]]
    assert record["state"] == "needs_identity"
    assert record["parts"][0]["calendar_id"] == ""
    await store.context.identify_drop({"id": item["id"], "part": 0, "name": "Large"})
    await store.context.dropbox.process()
    for receipt in store.context.archive.receipts():
        assert store.context.archive.process(receipt.id).state == "succeeded"
    await store.context.dropbox.process()
    assert store.context.dropbox.records()["items"][item["id"]]["state"] == "succeeded"
    assert len(store.context.archive.catalog().events) == 12000


async def test_original_upload_over_10_mib_is_rejected_at_intake(tmp_path):
    store = local_store(tmp_path)
    with pytest.raises(ValueError, match="10 MiB"):
        store.context.dropbox.submit("calendar.ics", large_ics())
    assert store.context.dropbox.records()["items"] == {}


async def test_zip_expanding_past_40_mib_fails_the_receipt(tmp_path):
    store = local_store(tmp_path)
    source = zip_bytes({f"{n}.ics": large_ics(str(n)) for n in range(4)})
    item = store.context.dropbox.submit("export.zip", source)
    await store.context.dropbox.process()
    record = store.context.dropbox.records()["items"][item["id"]]
    assert record["state"] == "failed"
    assert "40 MiB" in record["error"]
