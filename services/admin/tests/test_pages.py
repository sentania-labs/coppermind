"""Every page draws from what the store says, and says so plainly when the store is silent."""

from __future__ import annotations

import re

import pytest

from services.admin.tests.harness import VIEWS
from services.admin.tests.harness import admin as admin

NOTE = VIEWS["note"]["id"]
PERSON = VIEWS["person"]["id"]
SERVICE = VIEWS["service"]["service"]["id"]
ITEM = VIEWS["incoming-item"]["id"]
APP = VIEWS["apps"]["items"][-1]["id"]

PAGES = [
    ("/admin", "Connect each thing once"),
    ("/admin/advanced", "Things you set once"),
    ("/admin/ai", "Choose what turns a recording into text"),
    ("/admin/setup/companion", "Add the companion to Obsidian"),
    ("/admin/notes", "Notes"),
    ("/admin/notes?tab=waiting", "Waiting for review"),
    ("/admin/notes?tab=alone", "Left alone on purpose"),
    ("/admin/notes/leave-alone", "Files Coppermind leaves alone"),
    (f"/admin/notes/{NOTE}", "A note, and what has happened to it."),
    (f"/admin/notes/{NOTE}", "The meeting summary"),
    ("/admin/incoming", "Recordings and imports"),
    (f"/admin/incoming/{ITEM}", "what has happened to it"),
    ("/admin/calendar", "Calendar files"),
    ("/admin/people", "People and voices"),
    ("/admin/people?tab=companies", "Companies"),
    ("/admin/people?tab=voices", "Voice recognition"),
    (f"/admin/people/{PERSON}", "What Coppermind knows"),
    ("/admin/sync", "Obsidian Sync"),
    ("/admin/sync/what", "What syncs"),
    ("/admin/services", "AI services"),
    ("/admin/services/new", "Add an AI service"),
    (f"/admin/services/{SERVICE}", "What it can do"),
    (f"/admin/services/{SERVICE}/remove", "Say what takes over"),
    ("/admin/suggestions", "Suggestions are on"),
    ("/admin/transcription/writeup", "How recordings are written up"),
    ("/admin/apps", "Connected apps"),
    ("/admin/apps/new", "Connect an app"),
    ("/admin/apps/pair", "Pair the companion on another device"),
    (f"/admin/apps/{APP}", "One connected app"),
    ("/admin/storage", "Storage"),
    ("/admin/storage/files", "Files beside your notes"),
    ("/admin/settings/timezone", "Time zone"),
    ("/admin/settings/signin", "Password and signing in"),
    ("/admin/settings/folders", "Folders"),
    ("/admin/settings/properties", "Note properties"),
    ("/admin/settings/properties/edit/date", "Edit"),
    ("/admin/settings/tags", "Tags"),
    ("/admin/settings/search", "Search"),
    ("/admin/settings/advanced", "Limits and timing"),
    ("/admin/settings/advanced?tab=about", "For support"),
]


@pytest.mark.parametrize(("path", "words"), PAGES, ids=[path for path, _ in PAGES])
def test_page_draws(admin, path, words):
    client, _, _ = admin
    response = client.get(path)
    assert response.status_code == 200, response.text[:600]
    assert words in response.text
    assert "Traceback" not in response.text
    # Nothing a person reads is left as a raw placeholder or an unescaped fragment.
    assert "{csrf}" not in response.text
    shown = strip_tags(response.text)
    for leak in ("{'", "[None", "<coroutine", "object at 0x", "KeyError", "datetime."):
        assert leak not in shown, f"{path} shows {leak!r}"


def test_nothing_promises_recording_from_the_companion(admin):
    """Bug 7: the companion reviews notes; it has no command to record or hand one in."""
    client, _, store = admin
    store.answers["incoming"] = {**VIEWS["incoming"], "items": []}
    paths = [path for path, _ in PAGES] + ["/admin/incoming"]
    for path in paths:
        shown = " ".join(strip_tags(client.get(path).text).split())
        assert "Record with the companion" not in shown, path
        found = re.search(
            r"companion(?:(?!Can:)[^.]){0,80}hand in recordings"
            r"|hand in recordings[^.]{0,40}Obsidian",
            shown,
        )
        assert not found, f"{path}: {found and found.group(0)}"


def strip_tags(page: str) -> str:
    page = re.sub(r"<(script|style)\b.*?</\1>", "", page, flags=re.S)
    return re.sub(r"<[^>]+>", " ", page)


def test_every_form_that_changes_something_carries_the_page_token(admin):
    client, _, _ = admin
    for path, _ in PAGES:
        page = client.get(path).text
        for form in re.findall(r'<form\b[^>]*method="post"[^>]*>.*?</form>', page, flags=re.S):
            assert 'name="csrf"' in form, path


def test_a_page_reads_the_overview_once(admin):
    client, _, store = admin
    client.get("/admin")
    assert len(store.asked("overview")) == 1


@pytest.mark.parametrize(
    "path",
    ["/admin", "/admin/notes", "/admin/suggestions", "/admin/storage", "/admin/people"],
)
def test_a_silent_store_is_said_in_words_and_nothing_is_guessed(admin, path):
    client, _, store = admin
    store.down = True
    response = client.get(path)
    assert response.status_code in {200, 503}
    assert "Coppermind is not answering." in response.text
    assert "Your notes are safe." in response.text
    assert "Traceback" not in response.text


def test_a_fault_in_admin_is_said_in_words_too(admin):
    client, _, store = admin
    store.answers["suggestions"] = {"not": "what the page expects"}
    crashing = type(client)(
        client.app, base_url="https://testserver", raise_server_exceptions=False
    )
    crashing.cookies = client.cookies
    response = crashing.get("/admin/suggestions")
    assert response.status_code == 500
    assert "Something went wrong in Admin" in response.text
    assert "Your notes are not affected." in response.text
    assert "Traceback" not in response.text


def test_home_and_notes_never_count_suggestions_coppermind_was_unsure_of(admin):
    client, _, store = admin
    # A waiting note with suggestions left over from before notes arrived filled in.
    store.views["overview"]["notes"]["unsure"] = [{"id": NOTE, "title": "Vendor call", "count": 3}]
    for item in store.views["notes-waiting"]["items"]:
        item["unsure"] = 3
    store.views["note"]["unsure"] = 3
    for path in ("/admin", "/admin/notes", "/admin/notes?tab=waiting", f"/admin/notes/{NOTE}"):
        page = client.get(path).text
        assert "not sure about" not in page, path
        assert "3 to check" not in page, path
        assert "Check them" not in page, path


def test_a_phone_gets_the_same_pages(admin):
    client, _, _ = admin
    page = client.get("/admin").text
    assert 'name="viewport"' in page
    assert "width=device-width" in page


def test_a_reason_in_the_address_is_shown_as_text_never_run(admin):
    client, _, store = admin
    hostile = "<img src=x onerror=alert(1)>"
    store.views["overview"]["sync"].update(configured=False, state="not_connected")
    for path in (
        f"/admin/sync?field=email&problem={hostile}",
        f"/admin/sync?field=password&problem={hostile}",
        f"/admin/incoming?ask=folder&problem={hostile}",
    ):
        page = client.get(path).text
        assert "<img src=x" not in page, path
        assert "&lt;img src=x onerror=alert(1)&gt;" in page, path
