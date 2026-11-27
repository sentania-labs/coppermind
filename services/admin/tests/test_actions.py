"""Pressing a button: what Admin asks the store to do, and what it says back."""

from __future__ import annotations

import html
import re

import pytest

from coppermind.api_keys import add_key, load_keys
from coppermind.settings import read_settings
from coppermind.statefiles import StateStore
from coppermind.store_protocol import StoreError, ValidationFailed
from services.admin.tests.harness import VIEWS, follow, post, token
from services.admin.tests.harness import admin as admin

NOTE = VIEWS["note"]["id"]
PAGE = f"/admin/notes/{NOTE}"
REVIEW = f"{PAGE}/review"
EARLIER = {"summary": "The first summary.", "decisions": "", "action_items": ""}


# -- recording write-up directions ----------------------------------------------------------


def test_old_summary_editor_redirects_to_document_editor(admin):
    client, _, _ = admin
    response = client.get("/admin/suggestions/summary", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/transcription/writeup"


def test_document_editor_has_one_prompt_and_readonly_rules(admin):
    client, _, store = admin
    page = client.get("/admin/transcription/writeup").text
    # One prompt, plus the short list of names and terms to get right.
    assert page.count("<textarea") == 2
    assert 'name="writeup_prompt"' in page and 'name="writeup_glossary"' in page
    assert "Locked rules (read-only)" in page
    assert html.escape(store.views["writeup-settings"]["locked"]) in page


def test_writeup_editor_saves_exact_text_and_separate_limits(admin):
    client, _, store = admin
    form = {
        "writeup_prompt": "Keep it short.\nInclude commitments.",
        "writeup_glossary": "are back = RBAC",
        "connection": "writer",
        "model": "small",
        "enabled": "true",
        "max_output_tokens": "4096",
        "summary_max_input_chars": "200000",
        "timeout_s": "900",
    }
    result = post(client, "/admin/transcription/writeup", form)
    assert result.status_code == 303
    assert store.asked("writeup-save") == [{**form, "enabled": True}]


def test_writeup_reset_restores_default_without_changing_other_settings(admin):
    client, _, store = admin
    post(client, "/admin/transcription/writeup", {"reset": "true", "model": "unsaved"})
    assert store.asked("writeup-save") == [{"reset": True}]
    page = client.get("/admin/transcription/writeup").text
    assert "Directions reset to default. Your service, model and limits are unchanged." in page
    assert "Reset to default restores only the Directions text." in page
    assert "Write-up settings saved" not in page


# -- writing a summary again ----------------------------------------------------------------


def test_the_note_page_offers_to_write_the_summary_again(admin):
    client, _, store = admin
    page = client.get(PAGE).text
    assert "The meeting summary" in page
    assert "Write the summary again" in page
    assert f'action="{PAGE}/summary-again"' in page
    assert "the recording is not sent anywhere again" in " ".join(page.split())


def test_the_note_page_has_no_review_form_and_nothing_about_unsure_suggestions(admin):
    client, _, store = admin
    page = client.get(PAGE).text
    assert "not sure about" not in page
    assert "Nothing is chosen for you" not in page
    assert "Mark as reviewed and file it" not in page
    assert "Save, but do not file yet" not in page
    assert f'action="{PAGE}/review"' not in page
    assert 'name="how"' not in page


def test_the_old_review_address_lands_on_the_note_page(admin):
    client, _, store = admin
    response = client.get(REVIEW, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == PAGE
    refused = client.post(REVIEW, data={"csrf": token(client, PAGE)}, follow_redirects=False)
    assert refused.status_code in {404, 405}
    assert completed(store) == []


def test_writing_it_again_asks_the_store_and_says_so(admin):
    client, _, store = admin
    store.answers["note-summary-again"] = {"title": "Vendor call", "already": False}
    response = post(client, f"/admin/notes/{NOTE}/summary-again", page=PAGE)
    assert response.headers["location"] == PAGE
    page = follow(client, response)
    assert store.asked("note-summary-again") == [{"id": NOTE}]
    assert "writing the summary again from the same transcript" in page


def test_while_it_is_being_written_the_page_says_so_and_updates_itself(admin):
    client, _, store = admin
    store.views["note-summary"].update(writing=True, proposed=None, earlier=EARLIER)
    page = client.get(PAGE).text
    assert "Coppermind is writing the summary now." in page
    assert 'data-refresh="8"' in page
    assert page.count("<div data-live>") == 1
    assert "Write the summary again" not in page


def test_a_reviewed_note_is_refused_in_words(admin):
    client, _, store = admin
    store.answers["note-summary-again"] = {
        "refused": {"field": "note", "message": "This note has already been reviewed."}
    }
    page = follow(client, post(client, f"/admin/notes/{NOTE}/summary-again", page=PAGE))
    assert "This note has already been reviewed." in page


def test_a_stopped_service_offers_test_now_and_edit_instead_of_the_button(admin):
    client, _, store = admin
    service = store.views["note-summary"]["service"]
    store.views["note-summary"]["state"] = "failing"
    page = client.get(PAGE).text
    assert f"{service['name']} is not answering" in page
    assert "Test now" in page
    assert "Edit this service" in page
    assert f"/admin/services/{service['id']}/test" in page
    assert "Write the summary again" not in page


def completed(store):
    return [payload for name, payload in store.context_calls if name == "complete-review"]


# -- notes ------------------------------------------------------------------------------------


def test_notes_without_suggestions_can_be_started_and_left_out_ones_are_named(admin):
    client, _, store = admin
    store.answers["suggestions-older"] = {
        "started": ["Budget review"],
        "left_out": [{"title": "Old one", "why": "It is not a note Coppermind can read."}],
    }
    page = follow(client, post(client, "/admin/suggestions/older", page="/admin/suggestions"))
    assert "Started on 1 note." in page
    assert "1 note was left out: It is not a note Coppermind can read." in page


def test_a_note_that_could_not_be_filed_offers_one_button_and_no_time_or_name_choices(admin):
    client, _, store = admin
    store.views["note"].update(stuck="notfiled", why="disk", reason="Coppermind could not write.")
    store.views["note"]["reviewed"] = True
    page = client.get(f"/admin/notes/{NOTE}").text
    assert "Not filed" in page and "Check again now" in page
    for gone in ("There was no set time", "Keep both", "Replace the older file", "Replace it"):
        assert gone not in page


def test_the_pages_that_asked_about_a_time_or_a_name_clash_are_gone(admin):
    client, _, store = admin
    store.views["note"].update(stuck="notfiled", why="disk", reason="Coppermind could not write.")
    for action in ("no-time", "keep-both", "replace"):
        gone = post(client, f"/admin/notes/{NOTE}/{action}", page=f"/admin/notes/{NOTE}")
        assert gone.status_code in {404, 405}
    assert not [name for name, _ in store.calls if name.startswith("note-")]


def test_a_note_about_to_be_filed_is_not_called_a_problem(admin):
    client, _, store = admin
    store.views["note"]["settling"] = True
    page = client.get(f"/admin/notes/{NOTE}").text
    assert "Reviewed, about to be filed" in page
    assert "Not filed" not in page


def test_a_note_gone_on_purpose_can_be_said_to_be(admin):
    client, _, store = admin
    store.views["note"].update(stuck="missing", gone=False, recent=False, can_restore=False)
    page = client.get(f"/admin/notes/{NOTE}").text
    assert "It is gone on purpose" in page
    # With no saved copy there is nothing to bring back.
    assert "Bring back" not in page
    assert "/admin/notes/restore" not in page
    store.answers["note-gone"] = {"title": "Vendor call"}
    page = follow(client, post(client, f"/admin/notes/{NOTE}/gone", page=f"/admin/notes/{NOTE}"))
    assert "is no longer listed as a problem. Nothing was deleted by this." in page
    store.views["note"].update(recent=True)
    assert "No longer in your vault" in client.get(f"/admin/notes/{NOTE}").text


# -- a feature whose AI service is gone -------------------------------------------------------


def test_a_feature_whose_service_was_removed_outside_admin_says_so_on_the_coppermind_page(admin):
    client, _, store = admin
    store.views["transcription"].update(service=None, state="failing")
    store.views["overview"]["trans"].update(service=None, state="failing")
    page = client.get("/admin").text
    assert "AI server that transcribes recordings is not answering" in page
    assert "Nothing is lost." in page
    assert 'href="/admin/ai"' in page


# -- adding the companion -----------------------------------------------------------------------


def place_companion(store):
    store.views["overview"]["companion"].update(placed=True, paired=[])
    store.answers["pairing-state"] = {}
    store.answers["pairing-start"] = {"code": "123456"}


def sync_configs(wiring, configs):
    control = StateStore(wiring.state_dir)
    state = control.read("settings")
    body = dict(state.body)
    body.setdefault("sync", {})["sync_configs"] = configs
    control.write("settings", body, if_revision=state.revision)


@pytest.mark.parametrize("configs", [[], ["app"], ["appearance", "core-plugin"]])
def test_the_companion_step_warns_when_sync_will_not_carry_it_to_the_devices(admin, configs):
    client, wiring, store = admin
    place_companion(store)
    sync_configs(wiring, configs)
    page = client.get("/admin/setup/companion").text
    assert "only carries the companion to your devices" in page
    assert 'href="/admin/sync/what"' in page
    assert "It reaches your devices with the next sync" not in page


def test_the_companion_step_says_nothing_of_the_kind_when_add_ons_are_synced(admin):
    client, wiring, store = admin
    place_companion(store)
    sync_configs(wiring, ["app", "community-plugin"])
    page = client.get("/admin/setup/companion").text
    assert "only carries the companion to your devices" not in page
    assert "It reaches your devices with the next sync" in page


def test_the_companion_step_does_not_hand_out_a_new_code_when_the_old_one_ran_out(admin):
    client, _, store = admin
    place_companion(store)
    store.answers["pairing-state"] = {
        "code": "",
        "done": None,
        "expires_at": "2026-11-12T10:00:00+00:00",
    }
    page = client.get("/admin/setup/companion").text
    assert "Press New code" in page
    assert "123456" not in page
    assert store.asked("pairing-start") == []


def test_the_companion_step_offers_a_new_code_after_a_paired_device_was_cut_off(admin):
    client, _, store = admin
    place_companion(store)
    store.answers["pairing-state"] = {
        "code": "",
        "done": {"device": "My iPhone", "at": "2026-11-12T10:05:00+00:00"},
        "expires_at": "2026-11-12T10:10:00+00:00",
    }
    page = client.get("/admin/setup/companion").text
    assert "Press New code" in page
    assert 'action="/admin/setup/companion/code"' in page
    assert "appears here after step 1" not in page
    assert store.asked("pairing-start") == []


def test_the_companion_step_starts_a_code_the_first_time_only(admin):
    client, _, store = admin
    place_companion(store)
    page = client.get("/admin/setup/companion").text
    assert "123 456" in page or "123456" in page
    assert len(store.asked("pairing-start")) == 1


# -- never saving a voice -----------------------------------------------------------------------


def test_never_saving_a_voice_asks_first_and_says_the_samples_go_for_good(admin):
    client, _, store = admin
    person = store.views["person"]
    person.update(never=False, samples=[{"title": "Standup", "at": "2026-11-01T10:00:00+00:00"}])
    path = f"/admin/people/{person['id']}"
    page = client.get(path).text
    assert not re.search(r"<input[^>]*data-auto", page)
    assert "ask=never" in page
    asked = client.get(path + "?ask=never").text
    dialog = asked.split('class="dialog"')[1]
    assert "deleted for good" in dialog
    assert f'action="{path}/never"' in dialog
    assert 'name="never" value="on"' in dialog
    assert store.asked("voice-never") == []


def test_turning_never_saving_off_needs_no_question(admin):
    client, _, store = admin
    person = store.views["person"]
    person.update(never=True, samples=[])
    page = client.get(f"/admin/people/{person['id']}").text
    assert re.search(r"<input[^>]*data-auto", page)
    assert "ask=never" not in page


# -- suggestions: changing only the model -------------------------------------------------------


def test_changing_the_model_of_a_service_that_stopped_answering_keeps_the_choice_of_service(admin):
    client, _, store = admin
    store.views["services"]["services"][0]["answering"] = False
    page = client.get("/admin/suggestions?change=model").text
    assert "is not answering right now" in page
    assert 'name="change" value="service"' in page
    chosen = store.views["services"]["services"][1]["id"]
    again = client.get(f"/admin/suggestions?step=2&svc={chosen}&change=service").text
    assert "cannot write suggestions" in again
    assert "Which model?" not in again


# -- tags and note properties -------------------------------------------------------------------


def test_adding_a_tag_that_exists_keeps_its_aliases_and_meaning(admin):
    client, wiring, _ = admin
    page = "/admin/settings/tags"
    saved = post(
        client,
        page + "/save",
        {"tag": "work", "also": "job", "why": "Anything for the day job"},
        page=page,
    )
    assert saved.status_code == 303
    again = follow(client, post(client, page + "/save", {"tag": "work", "was": ""}, page=page))
    assert "already on your list" in again
    schema = StateStore(wiring.state_dir).read("schema").body
    assert schema["tags"]["work"] == "Anything for the day job"
    assert schema["tag_aliases"] == {"job": "work"}


def test_a_tag_the_schema_refuses_is_said_in_plain_words_with_the_raw_text_behind_support(admin):
    client, _, _ = admin
    page = "/admin/settings/tags"
    shown = follow(client, post(client, page + "/save", {"tag": "follow up"}, page=page))
    assert "That tag could not be saved." in shown
    main, _, support = shown.partition("Details for support")
    assert "ValidationError" not in main and "validation error" not in main.lower()
    assert support


# -- files Coppermind leaves alone ------------------------------------------------------------


def test_choosing_a_folder_to_leave_alone_keeps_the_ones_already_chosen(admin):
    client, _, store = admin
    store.views["folders"] = {"folders": ["Archive", "Projects"]}
    opened = client.get("/admin/notes/leave-alone?d=1&folder=Archive&pick=1").text
    dialog = opened.split('class="dialog"')[1]
    inputs = re.findall(r"<input[^>]*>", dialog)
    assert any('name="folder"' in tag and 'value="Archive"' in tag for tag in inputs)
    assert any('name="picked"' in tag and 'value="Projects"' in tag for tag in inputs)
    assert not any('name="folder"' in tag and 'type="radio"' in tag for tag in inputs)
    chosen = client.get("/admin/notes/leave-alone?d=1&folder=Archive&set=add&picked=Projects").text
    assert 'name="folder" value="Archive"' in chosen
    assert 'name="folder" value="Projects"' in chosen


# -- connected apps ---------------------------------------------------------------------------


def test_a_new_key_is_shown_once_and_only_once(admin):
    client, wiring, _ = admin
    form = {"kind": "custom", "name_custom": "Home automation", "can_read": "on", "can_hand": "on"}
    made = post(client, "/admin/apps/new", form, page="/admin/apps/new")
    assert made.status_code == 303
    control = StateStore(wiring.state_dir)
    (record,) = [key for key in load_keys(control).keys if key.name == "Home automation"]
    shown = client.get(made.headers["location"])
    credential = shown.text.split('id="newkey">')[1].split("<")[0]
    assert credential.startswith(f"cm_{record.key_id}_")
    assert "only time the key" in shown.text.replace("\n", " ")
    for path in (
        made.headers["location"],
        "/admin/apps",
        "/admin/apps/new",
        f"/admin/apps/{record.key_id}",
    ):
        assert credential not in client.get(path).text
    assert credential not in control.path_for("keys").read_text(encoding="utf-8")


def test_reloading_the_page_that_showed_a_new_key_makes_no_other_key(admin):
    client, wiring, _ = admin
    form = {"kind": "agent", "name_agent": "Assistant"}
    made = post(client, "/admin/apps/new", form, page="/admin/apps/new")
    control = StateStore(wiring.state_dir)
    assert "newkey" in client.get(made.headers["location"]).text
    for _ in range(2):
        again = client.get(made.headers["location"], follow_redirects=False)
        assert again.status_code == 303
    assert [key.name for key in load_keys(control).keys].count("Assistant") == 1


def test_a_replaced_key_is_shown_once_and_a_reload_does_not_replace_it_again(admin):
    client, wiring, store = admin
    made = post(
        client,
        "/admin/apps/new",
        {"kind": "agent", "name_agent": "Assistant"},
        page="/admin/apps/new",
    )
    client.get(made.headers["location"])
    control = StateStore(wiring.state_dir)
    (record,) = [key for key in load_keys(control).keys if key.name == "Assistant"]
    store.answers["apps"] = {
        "items": [
            {
                "id": record.key_id,
                "name": "Assistant",
                "companion": False,
                "device": "",
                "can": "Read notes",
                "permissions": ["read"],
                "used": "",
                "created": record.created_at if hasattr(record, "created_at") else "",
            }
        ]
    }
    path = f"/admin/apps/{record.key_id}"
    replaced = post(client, path + "/replace", {}, page=path)
    assert replaced.status_code == 303
    after_replace = load_keys(control).keys
    shown = client.get(replaced.headers["location"]).text
    credential = shown.split('id="newkey">')[1].split("<")[0]
    assert credential.startswith(f"cm_{record.key_id}_")
    reloaded = client.get(replaced.headers["location"]).text
    assert credential not in reloaded and "newkey" not in reloaded
    assert load_keys(control).keys == after_replace


def test_a_key_needs_a_name_and_something_it_may_do(admin):
    client, wiring, _ = admin
    before = len(load_keys(StateStore(wiring.state_dir)).keys)
    refused = post(
        client, "/admin/apps/new", {"kind": "custom", "name_custom": ""}, "/admin/apps/new"
    )
    assert refused.status_code == 422
    assert "Give it a name so you can recognise it later." in refused.text
    assert "Tick at least one thing it may do." in refused.text
    assert len(load_keys(StateStore(wiring.state_dir)).keys) == before


def _command_line_key(client, wiring, store, scopes):
    control = StateStore(wiring.state_dir)
    add_key(control, "Made on the command line", scopes, if_revision=load_keys(control).revision)
    (record,) = [k for k in load_keys(control).keys if k.name == "Made on the command line"]
    store.views["apps"]["items"].append(
        {
            "can": "Limited",
            "companion": False,
            "created": "2026-11-12T16:38:33+00:00",
            "device": "",
            "id": record.key_id,
            "name": record.name,
            "permissions": [],
            "starter": False,
            "used": "",
        }
    )
    return control, record


def test_renaming_a_key_keeps_scopes_the_ticks_do_not_cover(admin):
    client, wiring, store = admin
    control, record = _command_line_key(client, wiring, store, ["notes:read", "admin:purge"])
    page = f"/admin/apps/{record.key_id}"
    done = post(client, page + "/save", {"name": "Renamed"}, page=page)
    assert done.status_code == 303
    (after,) = [k for k in load_keys(control).keys if k.key_id == record.key_id]
    assert after.name == "Renamed"
    assert after.scopes == ["notes:read", "admin:purge"]


def test_ticking_a_permission_adds_its_scopes_and_unticking_removes_only_that_one(admin):
    client, wiring, store = admin
    control, record = _command_line_key(client, wiring, store, ["notes:read", "admin:purge"])
    page = f"/admin/apps/{record.key_id}"
    post(client, page + "/save", {"name": "Reader", "can_search": "on"}, page=page)
    (after,) = [k for k in load_keys(control).keys if k.key_id == record.key_id]
    assert after.scopes == ["notes:read", "search:read", "admin:purge"]
    store.views["apps"]["items"][-1]["permissions"] = ["search"]
    post(client, page + "/save", {"name": "Reader"}, page=page)
    (after,) = [k for k in load_keys(control).keys if k.key_id == record.key_id]
    assert after.scopes == ["notes:read", "admin:purge"]


# -- adding an AI service that is already there ----------------------------------------------


def test_a_service_already_in_the_list_is_linked_not_shown_as_markup(admin):
    client, _, store = admin
    store.answers["service-add"] = {
        "refused": {
            "field": "address",
            "message": 'That server is already in the list as "Home". A service is entered once.',
            "id": "conn_home",
        }
    }
    refused = post(
        client,
        "/admin/services/new",
        {"kind": "own", "address": "home:8000"},
        "/admin/services/new",
    )
    assert refused.status_code == 422
    assert 'href="/admin/services/conn_home"' in refused.text
    assert "&lt;a" not in refused.text
    assert "A service is entered once." in refused.text


# -- files handed in from this computer -------------------------------------------------------


def test_a_calendar_file_is_streamed_to_the_store_as_it_is(admin):
    client, _, store = admin
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
        data={"csrf": token(client, "/admin/calendar?ask=add")},
        files={"file": ("team.ics", b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", "text/calendar")},
        follow_redirects=False,
    )
    assert store.calendar_files == [("team.ics", b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")]
    page = follow(client, sent)
    assert "It holds 3 meetings, all in Nov 2026." in page


def _limit(wiring, **limits):
    control = StateStore(wiring.state_dir)
    state = control.read("settings")
    body = dict(state.body)
    body.setdefault("limits", {}).update(limits)
    control.write("settings", body, if_revision=state.revision)


def _add_file(client, path, name, size, kind="application/octet-stream"):
    return client.post(
        path,
        data={"csrf": token(client, "/admin/incoming")},
        files={"file": (name, b"x" * size, kind)},
        follow_redirects=False,
    )


def test_a_document_over_the_other_files_limit_is_refused_whatever_page_it_came_from(admin):
    client, wiring, store = admin
    _limit(wiring, artifact_max_bytes=10_000)
    for path, back in (
        ("/admin/incoming/add", "/admin/incoming"),
        ("/admin/storage/files/add", "/admin/storage/files"),
    ):
        refused = _add_file(client, path, "slides.pdf", 100_000, "application/pdf")
        assert refused.headers["location"].startswith(back)
        assert "That file is larger than 10 KB" in follow(client, refused)
    assert store.asked("incoming-accept") == []


def test_each_kind_of_file_is_held_to_its_own_limit(admin):
    client, wiring, store = admin
    store.answers["calendar-add"] = {
        "state": "added",
        "name": "team.ics",
        "has": 1,
        "new": 1,
        "from": "2026-11-10T16:00:00+00:00",
        "to": "2026-11-10T17:00:00+00:00",
        "total": 1,
    }
    _limit(wiring, artifact_max_bytes=10_000, staged_max_bytes=50_000)
    too_big = _add_file(client, "/admin/incoming/add", "standup.m4a", 100_000, "audio/mp4")
    assert "That file is larger than 50 KB" in follow(client, too_big)
    calendar = _add_file(client, "/admin/incoming/add", "team.ics", 100_000, "text/calendar")
    assert calendar.status_code == 303
    assert [name for name, _ in store.calendar_files] == ["team.ics"]


def test_a_file_without_the_page_token_is_not_taken(admin):
    client, _, store = admin
    refused = client.post(
        "/admin/calendar/add",
        data={"csrf": "0" * 64},
        files={"file": ("team.ics", b"BEGIN:VCALENDAR\r\n", "text/calendar")},
        follow_redirects=False,
    )
    assert store.calendar_files == []
    assert "Reload the page and add the file again." in follow(client, refused)


# -- sync -------------------------------------------------------------------------------------


def test_signing_in_again_asks_only_for_the_password_once_the_account_is_known(admin):
    client, _, store = admin
    sync = store.views["overview"]["sync"]
    sync.update(needs_signin=True, state="error", account_known=True)
    page = client.get("/admin/sync").text
    assert "Sign in again to restart sync" in page
    assert 'name="password"' in page
    assert 'name="email"' not in page
    sync["account_known"] = False
    asked_once = client.get("/admin/sync").text
    assert 'name="email"' in asked_once
    assert "remembers it for next time" in asked_once


# -- what a refused button says --------------------------------------------------------------


def test_a_store_that_did_not_answer_is_said_in_plain_words_with_the_raw_error_behind_support(
    admin,
):
    client, _, store = admin
    raw = "ConnectError: All connection attempts failed (errno 111)"
    store.answers["incoming-retry"] = StoreError(raw)
    page = follow(
        client,
        post(client, "/admin/incoming/rec1/retry", {"back": "/admin/incoming"}, "/admin/incoming"),
    )
    shown, _, behind = page.partition("Details for support")
    assert "That recording could not be tried again. Coppermind did not finish it." in shown
    assert "try again" in shown
    assert raw not in shown
    assert raw in behind


def test_a_refusal_the_store_worded_for_a_person_is_shown_as_it_is(admin):
    client, _, store = admin
    sentence = "That recording is already being written up."
    store.answers["incoming-retry"] = ValidationFailed([sentence])
    page = follow(
        client,
        post(client, "/admin/incoming/rec1/retry", {"back": "/admin/incoming"}, "/admin/incoming"),
    )
    assert f"That recording could not be tried again. {sentence}" in page
    assert "Coppermind did not finish it." not in page


# -- pausing, waits, models, clean-up, tags and removing a service --------------------------------


def test_changing_the_model_of_a_paused_service_says_it_is_still_paused(admin):
    client, _, store = admin
    store.views["suggestions"]["state"] = "paused"
    store.answers["suggestions-save"] = {
        "was": "paused",
        "model": "qwen2.5-14b",
        "service": "Home AI server",
        "resumed": {},
    }
    paused = follow(
        client,
        post(
            client, "/admin/suggestions/model", {"model": "qwen2.5-14b"}, page="/admin/suggestions"
        ),
    )
    assert "still paused" in paused
    store.views["suggestions"]["state"] = "on"
    running = follow(
        client,
        post(
            client, "/admin/suggestions/model", {"model": "qwen2.5-14b"}, page="/admin/suggestions"
        ),
    )
    assert "still paused" not in running


def selected(page, name):
    block = re.search(rf'<select[^>]*name="{name}".*?</select>', page, re.S)
    assert block is not None
    return re.findall(r'<option value="(\d+)"( selected)?', block[0])


def test_a_wait_the_store_would_refuse_is_shown_as_the_next_listed_one(admin):
    client, _, store = admin
    store.views["transcription"]["wait_s"] = 5400
    store.views["suggestions"]["wait_s"] = 90
    trans = selected(client.get("/admin/settings/advanced").text, "seconds")
    assert [value for value, _ in trans] == ["1800", "3600", "7200", "14400"]
    assert [value for value, mark in trans if mark] == ["7200"]
    sugg = selected(client.get("/admin/suggestions").text, "seconds")
    assert [value for value, mark in sugg if mark] == ["120"]


def test_a_model_that_failed_its_test_cannot_be_picked(admin):
    client, _, store = admin
    service = store.views["services"]["services"][0]
    service["text"]["models"][1]["ok"] = False
    page = client.get(f"/admin/suggestions?step=2&svc={service['id']}&change=service").text
    assert 'value="llama-3.1-8b"' in page
    assert 'value="qwen2.5-14b"' not in page
    assert "qwen2.5-14b: did not give a usable answer" in page
    final = client.get(
        f"/admin/suggestions?step=3&svc={service['id']}&model=qwen2.5-14b&change=service"
    ).text
    assert re.search(r'name="model" value="llama-3.1-8b"', final)


def test_a_files_page_address_with_a_bad_count_still_draws(admin):
    client, _, _ = admin
    for odd in ("abc", "\u00b2", "\u2460", "-3", ""):
        assert client.get("/admin/storage/files", params={"shown": odd}).status_code == 200


def test_removing_a_tag_asks_first_and_says_its_other_spellings_go_with_it(admin):
    client, _, _ = admin
    page = "/admin/settings/tags"
    post(client, page + "/save", {"tag": "work", "also": "job", "why": ""}, page=page)
    listing = client.get(page).text
    assert 'action="/admin/settings/tags/remove"' not in listing
    asked = client.get(page + "?ask=work").text
    dialog = asked.split('class="dialog"')[1]
    assert 'action="/admin/settings/tags/remove"' in dialog
    assert "job" in dialog and "other spellings" in dialog


def test_a_refusal_about_transcription_is_not_shown_under_suggestions(admin):
    client, _, store = admin
    services = store.views["services"]["services"]
    services[0]["used_by"] = ["sugg", "trans"]
    store.answers["service-remove"] = {
        "refused": {"field": "rm_trans", "message": "Home transcription is not answering."}
    }
    form = {"rm_sugg": services[1]["id"], "rm_trans": services[1]["id"]}
    path = f"/admin/services/{services[0]['id']}/remove"
    response = post(client, path, form, page=path)
    assert response.status_code == 422
    assert response.text.count("Home transcription is not answering.") == 1


def test_the_live_summary_refresh_keeps_the_choice_the_owner_made(tmp_path):
    import json
    import subprocess

    from coppermind_admin.layout import KEEP_CHOICES

    program = (
        KEEP_CHOICES
        + """
const radio=(name,value,checked)=>({name,value,checked});
const part=(radios,details)=>({
  querySelectorAll:(sel)=>sel==="input[type=radio]:checked"?radios.filter(r=>r.checked)
    :sel==="input[type=radio]"?radios:sel==="details"?details:[]});
const mine=[radio("summary","earlier",false),radio("summary","none",true)];
const fresh=[radio("summary","earlier",true),radio("summary","none",false)];
const other=[radio("how","file",true)];
const openNow=[{open:true},{open:false}], closedNew=[{open:false},{open:false}];
keepChoices(part(mine,openNow),part(fresh,closedNew));
const gone=[radio("summary","new",true)];
keepChoices(part(mine,[]),part(gone,[]));
console.log(JSON.stringify({fresh:fresh.map(r=>r.checked),gone:gone.map(r=>r.checked),
  details:closedNew.map(d=>d.open),other:other.map(r=>r.checked)}));
"""
    )
    script = tmp_path / "keep.cjs"
    script.write_text(program)
    done = subprocess.run(["node", str(script)], capture_output=True, text=True, check=True)
    result = json.loads(done.stdout)
    assert result["fresh"] == [False, True]
    assert result["gone"] == [True]
    assert result["details"] == [True, False]


# -- a file from the drop folder that could not be taken in ----------------------------------

RAW_DROP_ERROR = "OSError: [Errno 28] No space left on device: '/data/staged/x'"
REFUSED_DROP: dict[str, str | bool] = {
    "id": "d:" + "ab" * 32,
    "kind": "file",
    "title": "standup.m4a",
    "when": "2026-11-12T17:00:00+00:00",
    "state": "failed",
    "error": RAW_DROP_ERROR,
    "unsupported": False,
}


def refused_drop(store):
    store.views["incoming"]["items"] = [REFUSED_DROP, *store.views["incoming"]["items"]]
    store.views["incoming-item"] = dict(REFUSED_DROP)
    store.views["overview"]["incoming"]["failed"] = [REFUSED_DROP]


def test_a_refused_drop_is_a_file_with_what_to_do_and_no_try_again(admin):
    client, _, store = admin
    refused_drop(store)
    listing = client.get("/admin/incoming").text
    assert RAW_DROP_ERROR not in listing
    assert "Could not be taken in" in listing
    row = listing[listing.index("standup.m4a") :].split("</tr>")[0]
    assert "What to do" in row and "/retry" not in row


def test_a_refused_drop_opens_with_a_plain_sentence_and_remove(admin):
    client, _, store = admin
    refused_drop(store)
    page = client.get("/admin/incoming/" + str(REFUSED_DROP["id"]).replace(":", "%3A"))
    assert page.status_code == 200
    text = page.text
    assert "Remove" in text and "Add the new file" in text
    main, _, support = text.partition("Details for support")
    shown = html.escape(RAW_DROP_ERROR)
    assert shown in support and shown not in main
    assert (
        client.get(
            "/admin/incoming/" + str(REFUSED_DROP["id"]).replace(":", "%3A") + "?ask=remove"
        ).status_code
        == 200
    )


def test_a_key_with_access_no_box_covers_says_so_on_its_page(admin):
    client, wiring, store = admin
    _, record = _command_line_key(client, wiring, store, ["notes:read", "admin:purge"])
    page = client.get(f"/admin/apps/{record.key_id}").text
    assert "Also allowed outside Admin" in page
    shown = page[page.index("Also allowed outside Admin") :].split("</p>")[0]
    assert "admin:purge" in shown and "notes:read" in shown


def test_a_key_every_box_covers_has_no_outside_note(admin):
    client, wiring, store = admin
    _, record = _command_line_key(client, wiring, store, ["notes:read", "sources:read"])
    store.views["apps"]["items"][-1]["permissions"] = ["read"]
    page = client.get(f"/admin/apps/{record.key_id}").text
    assert "Also allowed outside Admin" not in page


# -- a form drawn before the thing it edits changed ------------------------------------------

STALE_SENTENCE = "This changed since you opened it. Reload and try again."


def hidden_value(page, name):
    match = re.search(rf'name="{name}" value="([^"]*)"', page)
    assert match is not None, f"no {name} field on the page"
    return html.unescape(match[1])


def bump(control, name):
    state = control.read(name)
    control.write(name, dict(state.body), if_revision=state.revision)


def test_a_time_zone_saved_from_a_stale_form_is_refused_in_a_plain_sentence(admin):
    client, wiring, _ = admin
    control = StateStore(wiring.state_dir)
    page = client.get("/admin/settings/timezone").text
    bump(control, "settings")
    sent = {
        "zone": "America/Denver",
        "rev": hidden_value(page, "rev"),
        "csrf": hidden_value(page, "csrf"),
    }
    done = client.post("/admin/settings/timezone", data=sent, follow_redirects=False)
    assert done.status_code == 303
    assert STALE_SENTENCE in client.get(done.headers["location"]).text
    assert read_settings(control).general.timezone != "America/Denver"


def test_a_time_zone_saved_from_a_fresh_form_is_kept(admin):
    client, wiring, _ = admin
    control = StateStore(wiring.state_dir)
    page = client.get("/admin/settings/timezone").text
    sent = {
        "zone": "America/Denver",
        "rev": hidden_value(page, "rev"),
        "csrf": hidden_value(page, "csrf"),
    }
    client.post("/admin/settings/timezone", data=sent, follow_redirects=False)
    assert read_settings(control).general.timezone == "America/Denver"


def test_a_property_saved_from_a_stale_form_does_not_overwrite_the_newer_change(admin):
    client, wiring, _ = admin
    control = StateStore(wiring.state_dir)
    page = client.get("/admin/settings/properties/edit/new").text
    bump(control, "schema")
    before = control.read("schema").revision
    sent = {
        "name": "mood",
        "kind": "string",
        "why": "How it went",
        "rev": hidden_value(page, "rev"),
        "csrf": hidden_value(page, "csrf"),
    }
    done = client.post("/admin/settings/properties/edit/new", data=sent)
    assert done.status_code == 422
    assert STALE_SENTENCE in done.text
    assert control.read("schema").revision == before


def test_pressing_create_key_twice_makes_one_key(admin):
    client, wiring, _ = admin
    control = StateStore(wiring.state_dir)
    page = client.get("/admin/apps/new").text
    sent = {
        "kind": "agent",
        "name_agent": "Walk helper",
        "once": hidden_value(page, "once"),
        "csrf": hidden_value(page, "csrf"),
    }
    before = len(load_keys(control).keys)
    first = client.post("/admin/apps/new", data=sent, follow_redirects=False)
    second = client.post("/admin/apps/new", data=sent, follow_redirects=False)
    assert first.status_code == second.status_code == 303
    assert len(load_keys(control).keys) == before + 1
    shown = client.get("/admin/apps/key")
    assert shown.status_code == 200 and "Key created" in shown.text


def test_pressing_replace_key_twice_replaces_it_once(admin):
    client, wiring, store = admin
    _, record = _command_line_key(client, wiring, store, ["notes:read"])
    path = f"/admin/apps/{record.key_id}"
    page = client.get(path + "?ask=replace").text
    sent = {"once": hidden_value(page, "once"), "csrf": hidden_value(page, "csrf")}
    control = StateStore(wiring.state_dir)
    client.post(path + "/replace", data=sent, follow_redirects=False)
    (after,) = [k for k in load_keys(control).keys if k.key_id == record.key_id]
    hashed = after.hash
    client.post(path + "/replace", data=sent, follow_redirects=False)
    (again,) = [k for k in load_keys(control).keys if k.key_id == record.key_id]
    assert again.hash == hashed


def test_response_limit_is_visible_and_saved(admin):
    client, _, store = admin
    store.views["suggestions"]["max_output_tokens"] = 16384
    page = client.get("/admin/suggestions").text
    assert 'name="max_output_tokens"' in page
    assert 'value="16384" selected' in page
    assert 'aria-label="Longest answer the model may write" data-auto' in page
    assert "Long (16,384)" in page
    assert "Longest (65,536)" in page
    assert "3 failed." in page
    store.answers["suggestions-response-limit"] = {"max_output_tokens": 65536}
    response = post(client, "/admin/suggestions/response-limit", {"max_output_tokens": "65536"})
    assert store.asked("suggestions-response-limit") == [{"max_output_tokens": "65536"}]
    assert "Saved." in follow(client, response)


def test_response_limit_keeps_an_existing_custom_value(admin):
    client, _, store = admin
    store.views["suggestions"]["max_output_tokens"] = 12345
    page = client.get("/admin/suggestions").text
    assert 'value="12345" selected' in page
    assert "Current (12,345)" in page


def test_cutoff_error_links_to_suggestion_setting(admin):
    client, _, store = admin
    item = store.views["incoming-item"]
    item["state"] = "failed"
    item["error"] = (
        "The model ran out of response space. On the Suggestions page in Admin, raise "
        "Longest answer the model may write. <unsafe>"
    )
    page = client.get("/admin/incoming/" + item["id"]).text
    assert 'href="/admin/suggestions#answer-length">Longest answer the model may write</a>' in page
    assert "&lt;unsafe&gt;" in page
    assert "<unsafe>" not in page


def test_failed_writeup_reason_is_shown_once_with_an_editor_link(admin):
    from coppermind.recording_writeup import failure_message

    client, _, store = admin
    item = store.views["incoming-item"]
    item.update(state="failed", rule="writeup-invalid", error=failure_message("writeup-invalid"))
    page = client.get("/admin/incoming/" + item["id"]).text
    assert page.count("The writing service sent an answer") == 1
    assert 'href="/admin/transcription/writeup"' in page
    assert "boundary block" not in page


# -- telling another system when a recording fails ------------------------------------------


def test_the_alert_address_is_off_until_the_owner_sets_one(admin):
    client, _, store = admin
    page = client.get("/admin/incoming").text
    assert "Tell another system when a recording fails" in page
    assert "Off. Failures and notes without a summary are always shown in Obsidian" in page
    assert "/admin/incoming?ask=webhook" in page
    asking = client.get("/admin/incoming?ask=webhook").text
    assert 'name="url"' in asking and 'name="stuck_hours"' in asking and 'value="6"' in asking


def test_saving_an_alert_address_shows_where_it_goes_and_never_the_secret(admin):
    client, _, store = admin
    store.answers["alerts-save"] = lambda payload: {
        "on": True,
        "where": "https://hooks.example",
        "stuck_hours": int(payload.get("stuck_hours", 6)),
        "recent": [],
    }
    page = "/admin/incoming"
    sent = post(
        client,
        page + "/alerts",
        {"url": "https://hooks.example/a/secret?t=1", "stuck_hours": "3"},
        page=page,
    )
    assert sent.status_code == 303
    assert store.asked("alerts-save") == [
        {"url": "https://hooks.example/a/secret?t=1", "stuck_hours": "3"}
    ]
    shown = follow(client, sent)
    assert "Saved. Coppermind will send to https://hooks.example." in shown
    store.views["alerts"] = {
        "on": True,
        "where": "https://hooks.example",
        "stuck_hours": 3,
        "recent": [],
    }
    on = client.get("/admin/incoming").text
    assert "Sending to https://hooks.example" in on and "3 hours" in on
    assert "secret" not in on


def test_a_bad_alert_address_is_explained_beside_the_field_and_turning_it_off_works(admin):
    client, _, store = admin
    store.answers["alerts-save"] = {
        "refused": {"field": "url", "message": "Use a full web address.", "id": ""}
    }
    page = "/admin/incoming"
    sent = post(client, page + "/alerts", {"url": "nope", "stuck_hours": "6"}, page=page)
    assert sent.status_code == 303
    assert "ask=webhook" in sent.headers["location"]
    assert "Use a full web address." in client.get(sent.headers["location"]).text
    store.answers["alerts-save"] = {"on": False, "where": "", "stuck_hours": 6, "recent": []}
    off = post(client, page + "/alerts", {"url": ""}, page=page)
    assert store.asked("alerts-save")[-1] == {"url": ""}
    assert "Turned off. Coppermind sends nothing to another system." in follow(client, off)


# -- deleting is final: nothing is kept back to restore or clean up ---------------------------

GONE_WORDS = (
    "clean up",
    "clean-up",
    "keep time",
    "kept for recovery",
    "brought back",
    "bring back",
)


def _shown(page: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", page)).lower()


def test_notes_has_no_deleted_tab_and_never_asks_for_deleted_notes(admin):
    client, _, store = admin
    page = client.get("/admin/notes").text
    assert "tab=deleted" not in page
    assert ">Deleted" not in page
    # An old address falls back to the usual tab.
    old = client.get("/admin/notes?tab=deleted").text
    assert "Need you (" in old and "tab=deleted" not in old
    assert store.asked("notes-deleted") == []
    for words in GONE_WORDS:
        assert words not in _shown(page)
        assert words not in _shown(old)


def test_storage_shows_space_and_files_and_nothing_about_keeping_deleted_things(admin):
    client, _, store = admin
    page = client.get("/admin/storage").text
    shown = _shown(page)
    assert "kb used" in shown and "recordings:" in shown and "notes:" in shown
    assert "Files beside your notes" in page and 'href="/admin/storage/files"' in page
    for words in (*GONE_WORDS, "keep deleted things", "count every", "deleted note"):
        assert words not in shown, words
    for gone in ("cleanup", "/admin/storage/deleted", "/admin/storage/keep", "count-interval"):
        assert gone not in page
    asked = {name for name, _ in store.calls}
    assert asked.isdisjoint({"cleanup", "notes-deleted", "storage-keep"})


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/admin/storage/cleanup"),
        ("get", "/admin/storage/deleted"),
        ("post", "/admin/storage/keep"),
        ("post", "/admin/storage/count-interval"),
        ("post", "/admin/storage/cleanup/confirm"),
        ("post", "/admin/storage/cleanup/cancel"),
        ("post", "/admin/storage/cleanup/bring-back"),
        ("post", "/admin/notes/delete-for-good"),
    ],
)
def test_the_old_keep_and_clean_up_addresses_are_gone(admin, method, path):
    client, _, _ = admin
    answer = post(client, path) if method == "post" else client.get(path, follow_redirects=False)
    assert answer.status_code in {404, 405}


def test_home_has_no_clean_up_row(admin):
    client, _, _ = admin
    page = client.get("/admin").text
    assert "/admin/storage/cleanup" not in page
    for words in GONE_WORDS:
        assert words not in _shown(page)


def test_deleting_a_copy_or_replacing_a_file_says_it_is_final(admin):
    client, _, store = admin
    facts = {
        "name": "Vendor call",
        "folder": "Review",
        "path": "Review/Vendor call.md",
        "edited": "2026-11-12T17:22:40+00:00",
        "words": 12,
    }
    store.views["note"].update(
        stuck="copy",
        original=facts,
        copy={**facts, "name": "Vendor call 1", "path": "Review/Vendor call 1.md"},
        difference={"same": True, "only_copy": [], "only_original": [], "more": 0},
        original_filed=False,
    )
    asked = client.get(f"/admin/notes/{NOTE}?ask=original").text
    dialog = asked.split('class="dialog"')[1]
    assert "final in Coppermind" in dialog and "Git history" in dialog
    for words in GONE_WORDS:
        assert words not in _shown(asked)
    store.answers["note-copy"] = {"choice": "original", "original": "Vendor call", "copy": "x"}
    page = follow(
        client,
        post(client, f"/admin/notes/{NOTE}/copy", {"choice": "original"}, f"/admin/notes/{NOTE}"),
    )
    assert "The copy was deleted." in page
    assert "under Notes" not in page


@pytest.mark.parametrize("recent", [False, True])
def test_a_missing_note_with_a_saved_copy_can_be_brought_back(admin, recent):
    client, _, store = admin
    here = f"/admin/notes/{NOTE}"
    store.views["note"].update(stuck="missing", gone=False, recent=recent, can_restore=True)
    page = client.get(here).text
    assert "Missing" in page and "No longer in your vault" not in page
    assert "Bring back the last version" in page
    assert 'action="/admin/notes/restore"' in page
    assert "Coppermind still has the last version it saw." in page
    for words in ("keep time", "clean up", "under deleted", "deleted list", "moves to deleted"):
        assert words not in _shown(page)
    store.answers["note-restore"] = {"title": "Vendor call", "path": "Review/Vendor call.md"}
    landed = follow(client, post(client, "/admin/notes/restore", {"id": NOTE, "back": here}, here))
    assert store.asked("note-restore") == [{"id": NOTE}]
    assert 'Brought back "Vendor call". It is in' in landed
    assert "Deleted" not in landed.split("Brought back")[1].split("</")[0]


def test_bringing_back_a_note_that_cannot_be_says_so(admin):
    client, _, store = admin
    here = f"/admin/notes/{NOTE}"
    store.answers["note-restore"] = StoreError("no saved copy")
    landed = follow(client, post(client, "/admin/notes/restore", {"id": NOTE, "back": here}, here))
    assert "That note could not be brought back just now." in landed


# -- what suggestions fill in -----------------------------------------------------------------


def test_the_suggestions_page_does_not_ask_what_it_fills_in(admin):
    client, _, _ = admin
    page = client.get("/admin/suggestions").text
    assert "What it fills in" not in page
    assert "/admin/suggestions/fill" not in page
    assert "Also let the model read meeting descriptions from my calendar" in page
    assert 'action="/admin/suggestions/descr"' in page
    assert client.get("/admin/suggestions/fill").status_code == 404


def test_turning_suggestions_on_is_two_steps_and_sends_no_list_of_fields(admin):
    client, _, store = admin
    store.views["suggestions"].update(state="none", service=None, model="")
    service = store.views["services"]["services"][0]
    first = client.get("/admin/suggestions").text
    assert "Choose what it fills in" not in first
    assert "Pick a model" in first
    last = client.get(f"/admin/suggestions?step=2&svc={service['id']}").text
    assert 'action="/admin/suggestions/save"' in last
    assert "Turn on suggestions" in last
    assert 'name="fill_' not in last
    store.answers["suggestions-save"] = {
        "was": "none",
        "model": "llama-3.1-8b",
        "service": service["name"],
        "resumed": {},
    }
    page = follow(
        client,
        post(
            client,
            "/admin/suggestions/save",
            {"svc": service["id"], "model": "llama-3.1-8b"},
            page="/admin/suggestions",
        ),
    )
    assert store.asked("suggestions-save") == [
        {"connection": service["id"], "model": "llama-3.1-8b"}
    ]
    assert "Suggestions are on." in page


def test_reading_calendar_descriptions_is_saved_by_itself(admin):
    client, _, store = admin
    store.answers["suggestions-descr"] = {"descr": True}
    page = follow(
        client,
        post(client, "/admin/suggestions/descr", {"descr": "on"}, page="/admin/suggestions"),
    )
    assert store.asked("suggestions-descr") == [{"descr": True}]
    assert "The model also reads meeting descriptions" in page
    store.answers["suggestions-descr"] = {"descr": False}
    post(client, "/admin/suggestions/descr", {}, page="/admin/suggestions")
    assert store.asked("suggestions-descr")[-1] == {"descr": False}


def test_properties_and_tags_have_no_suggestions_tick(admin):
    client, _, store = admin
    for path in (
        "/admin/settings/properties",
        "/admin/settings/properties/edit/date",
        "/admin/settings/properties/edit/new",
        "/admin/settings/tags",
    ):
        page = client.get(path).text
        assert "Let suggestions" not in page, path
        assert "Suggestions fill it in" not in page, path
        assert "/admin/settings/tags/fill" not in page, path
    assert store.asked("suggestions-tick") == []


# -- voices are named on the card in Review, not here ---------------------------------------------


def test_people_and_voices_has_no_unnamed_voices_and_no_choice_of_how_sure_to_be(admin):
    client, _, _ = admin
    page = client.get("/admin/people?tab=voices").text
    for gone in (
        "Careful",
        "Balanced",
        "Eager",
        "How sure",
        "Voices not named yet",
        "Name this voice",
        "Someone new",
    ):
        assert gone not in page
    # What stays: the one switch and each saved voice.
    assert "Voice recognition" in page and "Saved voices" in page


@pytest.mark.parametrize("name", ["level", "name", "forget"])
def test_the_routes_that_named_or_forgot_an_unnamed_voice_are_gone(admin, name):
    client, _, store = admin
    sent = post(
        client,
        f"/admin/people/voices/{name}",
        {"csrf": token(client, "/admin/people?tab=voices"), "id": "unknown:1", "level": "Eager"},
    )
    assert sent.status_code in {404, 405}
    assert not [a for a in ("voice-level", "voice-name", "voice-forget") if store.asked(a)]
    assert "/admin/people/clip" not in client.get("/admin/people?tab=voices").text


def test_a_recording_and_a_note_page_say_nothing_about_unnamed_voices(admin):
    client, _, store = admin
    for path in (f"/admin/notes/{NOTE}", "/admin/incoming/" + store.views["incoming-item"]["id"]):
        assert "Voices not named yet" not in client.get(path).text
