"""Admin is two pages: connect each thing once on Coppermind, and see that it works.

The Advanced page opens every page that was kept. What was removed is gone, and nothing links
to it.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import pytest

from services.admin.tests.harness import VIEWS, follow, post, token
from services.admin.tests.harness import admin as admin
from services.admin.tests.test_pages import PAGES
from services.admin.tests.test_sync_page import helper as helper

TEXT = VIEWS["services"]["services"][0]
SPEECH = VIEWS["services"]["services"][1]
SYSTEM_WORDS = ("Needs attention", "Nothing needs you", "needs you", "Everything is working")
ADVANCED = [
    "/admin/settings/signin",
    "/admin/settings/timezone",
    "/admin/settings/folders",
    "/admin/notes/leave-alone",
    "/admin/settings/properties",
    "/admin/transcription/writeup",
    "/admin/services",
    "/admin/settings/search",
    "/admin/sync/what",
    "/admin/people",
    "/admin/incoming",
    "/admin/calendar",
    "/admin/apps",
    "/admin/storage",
    "/admin/settings/advanced",
    "/admin/settings/advanced?tab=about",
]


def main_of(page: str) -> str:
    return page.split('<main class="main" id="content">', 1)[1]


def links(page: str) -> list[str]:
    """Every address a page sends a person to: links and the places its forms post to."""
    return re.findall(r'(?:href|action)="(/[^"]*)"', main_of(page))


def line_of(page: str, label: str) -> str:
    """The words in one row of a card, with the tags taken out."""
    row = re.search(rf'<div class="row-l">{label}</div><div class="row-v">(.*?)</div>', page, re.S)
    assert row is not None, f"no {label} line"
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row[1])).strip()


def summarizing(store) -> None:
    """The write-up is set to the text server, as the AI page leaves it."""
    store.views["writeup-settings"]["assignment"].update(
        connection=TEXT["id"], model="llama-3.1-8b", enabled=True
    )


# -- the menu ------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [path for path, _ in PAGES], ids=[path for path, _ in PAGES])
def test_the_menu_is_two_entries_on_every_page(admin, path):
    client, _, _ = admin
    page = client.get(path).text
    nav = re.search(r'<nav class="nav" aria-label="Admin">(.*?)</nav>', page, re.S)
    assert nav is not None
    assert re.findall(r'<a href="([^"]+)"[^>]*>([^<]+)</a>', nav[1]) == [
        ("/admin", "Coppermind"),
        ("/admin/advanced", "Advanced"),
    ]


@pytest.mark.parametrize(
    ("path", "entry"),
    [
        ("/admin", "Coppermind"),
        ("/admin/sync", "Coppermind"),
        ("/admin/ai", "Coppermind"),
        ("/admin/services", "Coppermind"),
        ("/admin/advanced", "Advanced"),
        ("/admin/people", "Advanced"),
        ("/admin/settings/timezone", "Advanced"),
        ("/admin/transcription/writeup", "Advanced"),
    ],
)
def test_the_menu_marks_where_you_are(admin, path, entry):
    client, _, _ = admin
    nav = re.search(r'<nav class="nav" aria-label="Admin">(.*?)</nav>', client.get(path).text, re.S)
    assert nav is not None
    assert re.findall(r'class="on" aria-current="page">([^<]+)<', nav[1]) == [entry]


# -- what was removed ----------------------------------------------------------------------------

GONE = [
    "/admin/setup",
    "/admin/transcription",
    "/admin/settings",
    "/admin/calendar/be990a9fe3e2507326ca83a740c6362c7bd4255f7ff6a1cf1d8ba7a69863f5c7",
]
GONE_BUTTONS = [
    "/admin/setup/skip",
    "/admin/transcription/save",
    "/admin/transcription/pause",
    "/admin/transcription/wait",
    "/admin/transcription/tries",
    "/admin/transcription/retry",
]


@pytest.mark.parametrize("path", GONE)
def test_a_removed_page_is_not_found(admin, path):
    client, _, _ = admin
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("path", GONE_BUTTONS)
def test_a_removed_button_is_not_found(admin, path):
    client, _, _ = admin
    assert post(client, path, {}).status_code == 404


def is_removed(address: str) -> bool:
    path = urlsplit(address).path.rstrip("/")
    return (
        path in {"/admin/setup", "/admin/setup/skip", "/admin/settings", "/admin/transcription"}
        or (path.startswith("/admin/transcription/") and path != "/admin/transcription/writeup")
        or bool(re.fullmatch(r"/admin/calendar/[0-9a-f]{64}", path))
    )


@pytest.mark.parametrize("path", [path for path, _ in PAGES], ids=[path for path, _ in PAGES])
def test_nothing_links_to_a_removed_page(admin, path):
    client, _, _ = admin
    sent = [address for address in links(client.get(path).text) if is_removed(address)]
    assert sent == []


def test_the_coppermind_page_is_not_a_list_of_findings_tiles_and_a_feed(admin):
    client, _, store = admin
    store.views["overview"]["notes"]["unsure"] = [{"count": 3, "id": "n1", "title": "Vendor call"}]
    store.views["overview"]["incoming"]["failed"] = [
        {"id": "a:1", "kind": "recording", "title": "Standup", "error": "It did not finish."}
    ]
    page = client.get("/admin").text
    for gone in ("Recently", "not sure about", "Standup", "tile", "That is normal", "Setup guide"):
        assert gone not in page, gone
    for word in SYSTEM_WORDS:
        assert word not in page, word


# -- Obsidian ------------------------------------------------------------------------------------


def test_obsidian_says_it_is_syncing_and_when(admin):
    client, _, _ = admin
    said = line_of(client.get("/admin").text, "Obsidian Sync")
    assert said.startswith('Syncing "')
    assert "Last change" in said


def test_obsidian_can_be_connected_from_the_coppermind_page(admin):
    client, _, store = admin
    store.views["overview"]["sync"].update(configured=False, state="not_connected")
    page = client.get("/admin").text
    assert "Not connected. Connect Obsidian Sync" in line_of(page, "Obsidian Sync")
    assert 'href="/admin/sync"' in main_of(page)
    # The companion is offered once there is a vault to put it in.
    assert "Companion" not in re.findall(r'class="row-l">([^<]+)<', page)
    connect = client.get("/admin/sync").text
    assert 'action="/admin/sync/signin"' in connect
    assert "Back to Coppermind" in connect


def test_a_paired_device_without_a_name_is_still_named_in_the_companion_line(admin):
    client, _, store = admin
    paired = store.views["overview"]["companion"]["paired"]
    store.views["overview"]["companion"]["paired"] = [{**paired[0], "device": ""}]
    assert line_of(client.get("/admin").text, "Companion").startswith("On A device.")


def test_a_speech_server_added_from_the_ai_page_is_offered_for_the_ai_server(admin):
    client, _, store = admin
    store.answers["service"] = {"service": SPEECH}
    page = client.get(f"/admin/services/{SPEECH['id']}?for=ai&pick=1").text
    assert "cannot write suggestions" not in page
    assert f"/admin/ai?use={SPEECH['id']}" in page


@pytest.mark.parametrize(
    ("change", "says", "action"),
    [
        ({"needs_signin": True}, "Sync has stopped", "/admin/sync"),
        ({"paused": True}, "Paused", "/admin/sync/resume"),
    ],
)
def test_obsidian_says_what_is_wrong_in_one_sentence_with_one_action(admin, change, says, action):
    client, _, store = admin
    store.views["overview"]["sync"].update(change)
    page = client.get("/admin").text
    assert says in line_of(page, "Obsidian Sync")
    assert f'"{action}"' in page


def test_resuming_sync_lands_back_on_the_coppermind_page(admin, helper):
    client, _, store = admin
    store.views["overview"]["sync"].update(paused=True)
    response = post(client, "/admin/sync/resume", {"back": "/admin"})
    assert response.headers["location"] == "/admin"
    assert helper.asked("resume") == [{}]


def test_the_companion_line_names_the_devices_and_offers_another(admin):
    client, _, _ = admin
    page = client.get("/admin").text
    assert line_of(page, "Companion").startswith("On My iPhone.")
    assert "/admin/apps/pair?from=coppermind" in page
    assert "Back to Coppermind" in client.get("/admin/apps/pair?from=coppermind").text


@pytest.mark.parametrize(
    ("change", "says", "action"),
    [
        ({"placed": False, "paired": []}, "Not in your vault yet.", "Add the companion"),
        ({"paired": []}, "not paired with a device yet", "Pair a device"),
    ],
)
def test_the_companion_can_be_added_and_paired_from_the_coppermind_page(
    admin, change, says, action
):
    client, _, store = admin
    store.views["overview"]["companion"].update(change)
    store.answers["pairing-state"] = {"code": "123456", "expires_at": "2099-01-01T00:00:00+00:00"}
    page = client.get("/admin").text
    assert says in line_of(page, "Companion")
    assert action in page
    steps = client.get("/admin/setup/companion").text
    assert "Back to Coppermind" in steps


def test_the_update_notice_is_in_the_companion_line_and_can_be_put_away(admin):
    client, _, store = admin
    store.views["overview"]["notices"] = [
        {"id": "companion-updated", "kind": "companion_updated", "from": "0.9.1", "to": "0.9.3"}
    ]
    page = client.get("/admin").text
    assert "Updated from 0.9.1 to 0.9.3." in line_of(page, "Companion")
    gone = post(client, "/admin/notices/dismiss", {"id": "companion-updated"})
    assert gone.headers["location"] == "/admin"
    assert store.asked("notice-dismiss") == [{"id": "companion-updated"}]


# -- the AI server -------------------------------------------------------------------------------


def test_the_ai_card_says_it_is_working_and_what_each_job_uses(admin):
    client, _, store = admin
    summarizing(store)
    store.views["transcription"]["model"] = "diarize-1"
    page = client.get("/admin").text
    assert line_of(page, "AI server").startswith("Working.")
    assert line_of(page, "Transcribes with") == "diarize-1 on Home transcription"
    assert line_of(page, "Summarizes with") == "llama-3.1-8b on Home AI server"


def test_the_ai_card_is_one_line_when_one_server_does_both(admin):
    client, _, store = admin
    both = {**TEXT, "speech": {**SPEECH["speech"], "models": ["diarize-1"]}}
    store.views["services"]["services"] = [both]
    store.views["overview"]["services"] = [both]
    store.views["overview"]["trans"]["service"] = both
    store.views["transcription"].update(service=both, model="diarize-1")
    summarizing(store)
    page = client.get("/admin").text
    assert line_of(page, "Server") == "Home AI server"
    assert line_of(page, "Transcribes with") == "diarize-1"
    assert line_of(page, "Summarizes with") == "llama-3.1-8b"


def test_nothing_connected_offers_one_button_and_hides_the_rest(admin):
    client, _, store = admin
    store.views["overview"]["trans"].update(state="none", service=None)
    store.views["transcription"].update(state="none", service=None)
    page = client.get("/admin").text
    assert "Not connected. Connect an AI server" in line_of(page, "AI server")
    assert "Transcribes with" not in page
    assert 'href="/admin/ai"' in page


def test_an_ai_server_can_be_chosen_from_the_coppermind_page(admin):
    client, _, store = admin
    store.views["overview"]["trans"].update(state="none", service=None)
    store.views["transcription"].update(state="none", service=None)
    chooser = client.get("/admin/ai").text
    assert "Back to Coppermind" in chooser
    assert re.search(r'name="heard"', chooser) and re.search(r'name="written"', chooser)
    assert f'value="{SPEECH["id"]}|"' in chooser
    assert f'value="{TEXT["id"]}|llama-3.1-8b"' in chooser
    assert f'value="{TEXT["id"]}|qwen2.5-14b"' in chooser
    assert "/admin/services/new?for=ai&amp;pick=1" in chooser
    response = post(
        client,
        "/admin/ai/save",
        {"heard": f"{SPEECH['id']}|", "written": f"{TEXT['id']}|llama-3.1-8b"},
        "/admin/ai",
    )
    assert response.headers["location"] == "/admin"
    assert store.asked("transcription-save")[0]["connection"] == SPEECH["id"]
    assert store.asked("writeup-save") == [
        {"connection": TEXT["id"], "model": "llama-3.1-8b", "enabled": True}
    ]
    # Once the store holds that, the Coppermind page says it is working.
    store.views["overview"]["trans"].update(state="on", service=SPEECH)
    store.views["transcription"].update(state="on", service=SPEECH)
    summarizing(store)
    assert line_of(client.get("/admin").text, "AI server").startswith("Working.")


def test_the_ai_page_keeps_the_wait_already_chosen_and_never_picks_an_outside_server(admin):
    client, _, store = admin
    store.views["transcription"]["wait_s"] = 7200
    outside = {**TEXT, "id": "outside", "name": "Cloud", "outside": True}
    store.views["services"]["services"].append(outside)
    store.views["overview"]["trans"].update(state="none", service=None)
    store.views["transcription"].update(state="none", service=None)
    store.views["writeup-settings"]["assignment"].update(connection="", model="")
    chooser = client.get("/admin/ai").text
    found = re.search(r'<select[^>]*name="written".*?</select>', chooser, re.S)
    assert found is not None
    summary = found[0]
    assert "(outside your network)" in summary
    assert re.findall(r'<option value="[^"]*" selected>([^<]+)<', summary) == [
        "llama-3.1-8b on Home AI server"
    ]
    post(client, "/admin/ai/save", {"heard": f"{SPEECH['id']}|"}, "/admin/ai")
    assert store.asked("transcription-save")[0]["wait_s"] == 7200


def test_saving_nothing_on_the_ai_page_says_so_and_changes_nothing(admin):
    client, _, store = admin
    response = post(client, "/admin/ai/save", {"heard": "", "written": ""}, "/admin/ai")
    assert response.headers["location"] == "/admin/ai"
    assert "Choose what transcribes" in follow(client, response)
    assert store.asked("transcription-save") == [] == store.asked("writeup-save")


def test_a_server_that_is_not_answering_says_so_with_one_button_back_to_this_page(admin):
    client, _, store = admin
    summarizing(store)
    TEXT_DOWN = {**TEXT, "answering": False, "down_since": "2026-11-12T17:00:00+00:00"}
    store.views["overview"]["services"] = [TEXT_DOWN, SPEECH]
    store.views["services"]["services"] = [TEXT_DOWN, SPEECH]
    page = client.get("/admin").text
    said = line_of(page, "AI server")
    assert said.startswith("Home AI server is not answering.")
    assert "Nothing is lost." in said
    test = re.search(
        rf'<form class="act" method="post" action="/admin/services/{TEXT["id"]}/test">(.*?)</form>',
        page,
        re.S,
    )
    assert test is not None and 'value="/admin"' in test[1]
    store.answers["service-test"] = {"service": {**TEXT, "answering": True}, "answering": True}
    response = post(client, f"/admin/services/{TEXT['id']}/test", {"back": "/admin"})
    assert response.headers["location"] == "/admin"


def test_paused_transcription_is_one_sentence_and_one_button(admin):
    client, _, store = admin
    summarizing(store)
    store.views["overview"]["trans"]["state"] = "paused"
    store.views["transcription"]["state"] = "paused"
    page = client.get("/admin").text
    assert "Transcription is paused." in line_of(page, "AI server")
    response = post(client, "/admin/ai/resume", {"back": "/admin"})
    assert response.headers["location"] == "/admin"
    assert store.asked("transcription-pause") == [{"paused": False}]


def test_a_summary_that_nothing_writes_is_said_not_hidden(admin):
    client, _, _ = admin
    assert "nothing writes their summary" in line_of(client.get("/admin").text, "AI server")


# -- the calendar --------------------------------------------------------------------------------


def test_a_calendar_can_be_added_from_the_coppermind_page_and_the_line_updates(admin):
    client, _, store = admin
    store.views["overview"]["calendar"].update(files=0, events=0)
    assert "No calendar yet" in line_of(client.get("/admin").text, "Calendar")
    dialog = client.get("/admin?ask=calendar").text
    assert 'action="/admin/calendar/add"' in dialog
    assert 'name="back" value="/admin"' in dialog
    store.answers["calendar-add"] = {
        "state": "added",
        "name": "team.ics",
        "has": 3,
        "new": 3,
        "from": "2026-11-10T16:00:00+00:00",
        "to": "2026-11-12T13:00:00+00:00",
        "total": 3,
    }
    sent = client.post(
        "/admin/calendar/add",
        data={"csrf": token(client, "/admin?ask=calendar"), "back": "/admin"},
        files={"file": ("team.ics", b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", "text/calendar")},
        follow_redirects=False,
    )
    assert sent.headers["location"] == "/admin"
    assert store.calendar_files == [("team.ics", b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")]
    assert "It holds 3 meetings" in follow(client, sent)
    store.views["overview"]["calendar"].update(files=1, events=3)
    assert line_of(client.get("/admin").text, "Calendar") == "Up to date, with 3 meetings."


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"bad_files": [{"id": "f1", "name": "x.ics"}]}, "A calendar file could not be read."),
        ({"conflicts": [{"title": "Budget"}]}, "Two calendar files disagree about a meeting."),
        ({"unavailable": True}, "Not read just now."),
    ],
)
def test_a_calendar_that_needs_a_look_says_what_in_one_sentence(admin, change, says):
    client, _, store = admin
    store.views["overview"]["calendar"].update(change)
    assert says in line_of(client.get("/admin").text, "Calendar")


# -- Advanced ------------------------------------------------------------------------------------


def test_advanced_opens_every_page_that_was_kept(admin):
    client, _, _ = admin
    page = client.get("/admin/advanced").text
    for address in ADVANCED:
        assert address in links(page), address
        reached = client.get(address)
        assert reached.status_code == 200, address
        assert "Traceback" not in reached.text


def test_advanced_links_back_to_coppermind_and_nothing_else_is_a_menu_entry(admin):
    client, _, _ = admin
    page = client.get("/admin/advanced").text
    assert 'href="/admin"' in main_of(page)


def test_the_companion_update_switch_is_on_by_default_and_lives_on_advanced(admin):
    client, _, _ = admin
    advanced = client.get("/admin/advanced").text
    assert "Update the companion by itself" in advanced
    assert "It is <b>on</b>." in advanced
    assert "companion-auto" not in client.get("/admin/sync").text
    assert "companion-auto" not in client.get("/admin").text


def test_the_waits_for_a_recording_are_on_the_limits_page(admin):
    client, _, store = admin
    page = client.get("/admin/settings/advanced").text
    assert 'action="/admin/ai/wait"' in page and 'action="/admin/ai/tries"' in page
    store.answers["transcription-wait"] = {"seconds": 7200}
    store.answers["transcription-tries"] = {"tries": 3}
    waited = post(client, "/admin/ai/wait", {"seconds": "7200"}, "/admin/settings/advanced")
    assert store.asked("transcription-wait") == [{"seconds": 7200}]
    assert "2 hours" in follow(client, waited)
    tried = post(client, "/admin/ai/tries", {"tries": "3"}, "/admin/settings/advanced")
    assert store.asked("transcription-tries") == [{"tries": 3}]
    assert "3 times" in follow(client, tried)


# -- a phone -------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/admin", "/admin/advanced", "/admin/ai"])
def test_a_phone_gets_a_menu_button(admin, path):
    client, _, _ = admin
    page = client.get(path).text
    assert 'name="viewport" content="width=device-width,initial-scale=1"' in page
    assert 'data-menu aria-controls="side">Menu</button>' in page


def test_the_page_says_when_the_store_is_silent(admin):
    client, _, store = admin
    store.down = True
    page = client.get("/admin").text
    assert "Coppermind is not answering." in page
    for word in SYSTEM_WORDS:
        assert word not in page
