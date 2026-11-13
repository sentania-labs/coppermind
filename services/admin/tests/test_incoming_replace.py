"""Replacing a file that could not be read, and what a single upload may be."""

from __future__ import annotations

import tempfile
from pathlib import Path

from coppermind.store_protocol import StoreError
from services.admin.tests.harness import admin as admin
from services.admin.tests.harness import follow, token

DAMAGED = "c:" + "a" * 64
ICS = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
ADDED = {
    "state": "added",
    "name": "team.ics",
    "has": 1,
    "new": 1,
    "from": "2026-11-10T16:00:00+00:00",
    "to": "2026-11-10T17:00:00+00:00",
    "total": 1,
}


def replace(client, name, data, content_type):
    return client.post(
        "/admin/incoming/add",
        data={"csrf": token(client, "/admin/incoming"), "replaces": DAMAGED, "back": "/admin"},
        files={"file": (name, data, content_type)},
        follow_redirects=False,
    )


def test_a_file_of_the_wrong_kind_leaves_the_damaged_calendar_file_where_it_is(admin):
    client, _, store = admin
    store.answers["incoming-item"] = {"kind": "calendar", "id": DAMAGED, "state": "failed"}
    page = follow(client, replace(client, "notes.pdf", b"%PDF", "application/pdf"))
    assert "is not a calendar file" in page
    assert store.asked("incoming-replace-failed") == []
    assert store.asked("incoming-accept") == []
    assert store.calendar_files == []


def test_a_calendar_file_cannot_stand_in_for_a_damaged_document(admin):
    client, _, store = admin
    store.answers["incoming-item"] = {"kind": "document", "id": "d:x", "state": "failed"}
    page = follow(client, replace(client, "team.ics", ICS, "text/calendar"))
    assert "is a calendar file" in page
    assert store.asked("incoming-replace-failed") == []
    assert store.calendar_files == []


def test_a_calendar_file_of_the_right_kind_replaces_the_damaged_one(admin):
    client, _, store = admin
    store.answers["incoming-item"] = {"kind": "calendar", "id": DAMAGED, "state": "failed"}
    store.answers["calendar-add"] = ADDED
    store.answers["incoming-replace-failed"] = {"title": "old.ics", "kind": "refused"}
    page = follow(client, replace(client, "team.ics", ICS, "text/calendar"))
    assert store.asked("incoming-replace-failed") == [{"id": DAMAGED}]
    assert store.asked("incoming-remove") == []
    assert 'The damaged file "old.ics" was removed' in page


def test_adding_the_file_is_not_called_a_failure_when_clearing_the_old_one_fails(admin):
    client, _, store = admin
    store.answers["incoming-item"] = {"kind": "calendar", "id": DAMAGED, "state": "failed"}
    store.answers["calendar-add"] = ADDED
    store.answers["incoming-replace-failed"] = StoreError("the store did not answer")
    page = follow(client, replace(client, "team.ics", ICS, "text/calendar"))
    assert "could not be added" not in page
    assert [name for name, _ in store.calendar_files] == ["team.ics"]


def test_the_picker_for_a_damaged_calendar_file_only_offers_calendar_files(admin):
    client, _, store = admin
    store.answers["incoming-item"] = {
        "kind": "calendar",
        "id": DAMAGED,
        "state": "failed",
        "title": "old.ics",
        "when": "2026-11-12T16:00:00+00:00",
        "error": "cut short",
    }
    page = client.get(f"/admin/incoming/{DAMAGED}?ask=new").text
    assert 'accept=".ics,.zip,text/calendar"' in page


def test_a_request_carrying_two_files_is_refused_and_nothing_is_taken(admin):
    client, _, store = admin
    body = (
        b'--B\r\nContent-Disposition: form-data; name="csrf"\r\n\r\n'
        + token(client, "/admin/incoming").encode()
        + b'\r\n--B\r\nContent-Disposition: form-data; name="file"; filename="a.ics"\r\n'
        b"Content-Type: text/calendar\r\n\r\n" + ICS + b"\r\n"
        b'--B\r\nContent-Disposition: form-data; name="file"; filename="b.ics"\r\n'
        b"Content-Type: text/calendar\r\n\r\n" + ICS + b"\r\n--B--\r\n"
    )
    leftovers = set(Path(tempfile.gettempdir()).glob("coppermind-upload-*"))
    refused = client.post(
        "/admin/incoming/add",
        content=body,
        headers={"content-type": "multipart/form-data; boundary=B"},
        follow_redirects=False,
    )
    assert "Add one file at a time" in follow(client, refused)
    assert store.calendar_files == []
    assert set(Path(tempfile.gettempdir()).glob("coppermind-upload-*")) == leftovers


def test_a_file_whose_name_holds_a_semicolon_keeps_its_whole_name(admin):
    # A browser sends the name in quotes, so the semicolon is part of the name.
    client, _, store = admin
    store.answers["calendar-add"] = {**ADDED, "name": "Q3; review.ics"}
    sent = client.post(
        "/admin/incoming/add",
        data={"csrf": token(client, "/admin/incoming")},
        files={"file": ("Q3; review.ics", ICS, "text/calendar")},
        follow_redirects=False,
    )
    follow(client, sent)
    assert [name for name, _ in store.calendar_files] == ["Q3; review.ics"]


def test_text_inside_a_file_name_cannot_pass_itself_off_as_another_field(admin):
    client, _, store = admin
    store.answers["calendar-add"] = ADDED
    body = (
        b'--B\r\nContent-Disposition: form-data; name="csrf"\r\n\r\n'
        + token(client, "/admin/incoming").encode()
        + b'\r\n--B\r\nContent-Disposition: form-data; name="file"; '
        b'filename="a; name=csrf; x.ics"\r\n'
        b"Content-Type: text/calendar\r\n\r\n" + ICS + b"\r\n--B--\r\n"
    )
    sent = client.post(
        "/admin/incoming/add",
        content=body,
        headers={"content-type": "multipart/form-data; boundary=B"},
        follow_redirects=False,
    )
    follow(client, sent)
    assert [name for name, _ in store.calendar_files] == ["a; name=csrf; x.ics"]
