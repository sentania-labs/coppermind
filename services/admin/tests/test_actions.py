"""Pressing a button: what Admin asks the store to do, and what it says back."""

from __future__ import annotations

import re
from typing import Any

import pytest

from coppermind.api_keys import add_key, load_keys
from coppermind.statefiles import StateStore
from coppermind.store_protocol import StoreError, ValidationFailed
from services.admin.tests.harness import VIEWS, follow, post, token
from services.admin.tests.harness import admin as admin

NOTE = VIEWS["note"]["id"]
REVIEW = f"/admin/notes/{NOTE}/review"
MEETINGS = [c["event_id"] for c in VIEWS["lifecycle"]["recording_meeting"]["candidates"]]
EARLIER = {"summary": "The first summary.", "decisions": "", "action_items": ""}


# -- how the meeting summary is written -----------------------------------------------------


def test_the_page_has_one_box_of_directions_for_each_section_and_the_locked_rules(admin):
    client, _, store = admin
    page = client.get("/admin/suggestions/summary").text
    view = store.views["summary-directions"]
    for section in view["sections"]:
        assert f"Directions for {section['name']}" in page
        assert f'name="d_{section["key"]}"' in page
        assert section["default"].replace("'", "&#x27;") in page
        assert f'value="reset:{section["key"]}"' in page
    for rule in view["locked"]:
        assert rule in page
    assert "Always the same, whatever you write" in page
    assert "Takes effect with the next summary Coppermind writes." in page
    assert page.count(">Save</button>") == 1


def test_a_section_added_in_the_store_shows_up_without_a_change_to_the_page(admin):
    client, _, store = admin
    store.views["summary-directions"]["sections"].append(
        {
            "key": "risks",
            "name": "Open risks",
            "text": "One line each.",
            "default": "One line each.",
            "custom": False,
        }
    )
    page = client.get("/admin/suggestions/summary").text
    assert "Directions for Open risks" in page
    assert 'name="d_risks"' in page
    store.answers["summary-directions-save"] = {"custom": ["risks"], "emptied": []}
    form = {"how": "save", "d_summary": "A", "d_decisions": "B", "d_action_items": "C"}
    post(client, "/admin/suggestions/summary", form | {"d_risks": "Worst first."})
    assert store.asked("summary-directions-save")[0]["directions"]["risks"] == "Worst first."


def test_saving_sends_exactly_what_was_typed_for_each_section(admin):
    client, _, store = admin
    store.answers["summary-directions-save"] = {"custom": ["summary"], "emptied": []}
    form = {"how": "save", "d_summary": "Two lines at most.", "d_decisions": "As they were."}
    saved = post(client, "/admin/suggestions/summary", form | {"d_action_items": "Mine first."})
    assert store.asked("summary-directions-save") == [
        {
            "directions": {
                "summary": "Two lines at most.",
                "decisions": "As they were.",
                "action_items": "Mine first.",
            }
        }
    ]
    page = follow(client, saved)
    assert "Saved. The next summary is written with your directions for Summary." in page
    assert "Summaries already written were not changed." in page


def test_an_empty_box_is_said_to_mean_the_usual_directions(admin):
    client, _, store = admin
    store.answers["summary-directions-save"] = {"custom": [], "emptied": ["Decisions"]}
    form = {"how": "save", "d_summary": "x", "d_decisions": "", "d_action_items": "y"}
    page = follow(client, post(client, "/admin/suggestions/summary", form))
    assert "The box for Decisions was empty, so it uses its usual directions." in page


def test_directions_that_are_too_long_come_back_as_typed_with_the_reason(admin):
    client, _, store = admin
    store.answers["summary-directions-save"] = {
        "refused": {"field": "decisions", "message": "The directions for Decisions are too long."}
    }
    form = {"how": "save", "d_summary": "kept", "d_decisions": "my words", "d_action_items": "z"}
    refused = post(client, "/admin/suggestions/summary", form)
    assert refused.status_code == 422
    assert "The directions for Decisions are too long." in refused.text
    assert ">my words</textarea>" in refused.text
    assert ">kept</textarea>" in refused.text


def test_reset_puts_one_default_back_in_its_box_and_saves_nothing(admin):
    client, _, store = admin
    default = store.views["summary-directions"]["sections"][1]["default"]
    form = {"how": "reset:decisions", "d_summary": "my summary words", "d_decisions": "mine"}
    page = post(client, "/admin/suggestions/summary", form | {"d_action_items": "also mine"})
    assert page.status_code == 200
    assert store.asked("summary-directions-save") == []
    assert "The usual directions for Decisions are back in the box." in page.text
    assert "Nothing is saved until you press Save." in page.text
    assert f">{default}</textarea>" in page.text
    # What was typed in the other boxes is still there to be saved.
    assert ">my summary words</textarea>" in page.text
    assert ">also mine</textarea>" in page.text


def test_the_suggestions_page_says_whether_the_directions_are_the_owners_own(admin):
    client, _, store = admin
    assert "The usual directions for every section." in client.get("/admin/suggestions").text
    store.views["suggestions"]["summary_custom"] = True
    assert "Your own directions for at least one section." in (
        client.get("/admin/suggestions").text
    )


# -- writing a summary again ----------------------------------------------------------------


def test_the_review_shows_the_summary_and_offers_to_write_it_again(admin):
    client, _, store = admin
    page = client.get(REVIEW).text
    proposed = store.views["note-summary"]["proposed"]
    assert proposed["summary"] in page
    assert "Write the summary again" in page
    assert f'formaction="{REVIEW.removesuffix("/review")}/summary-again"' in page
    assert "the recording is\nnot sent anywhere again" in page.replace("  ", " ") or (
        "the recording is not sent anywhere again" in page
    )
    assert "Nothing is written to the note until you mark it reviewed." in page.replace("\n", " ")


def test_writing_it_again_asks_the_store_and_says_the_old_one_is_kept(admin):
    client, _, store = admin
    store.answers["note-summary-again"] = {"title": "Vendor call", "already": False}
    page = follow(client, post(client, f"/admin/notes/{NOTE}/summary-again", page=REVIEW))
    assert store.asked("note-summary-again") == [{"id": NOTE}]
    assert "writing the summary again from the same transcript" in page
    assert "The one you had is kept" in page


def test_while_it_is_being_written_the_page_says_so_and_updates_itself(admin):
    client, _, store = admin
    store.views["note-summary"].update(writing=True, proposed=None, earlier=EARLIER)
    page = client.get(REVIEW).text
    assert "Coppermind is writing the summary now." in page
    assert 'data-refresh="8"' in page
    assert "The summary from before is kept." in page
    assert "Write the summary again" not in page


def test_the_earlier_summary_stays_on_offer_until_the_note_is_reviewed(admin):
    client, _, store = admin
    store.views["note-summary"]["earlier"] = EARLIER
    page = client.get(REVIEW).text
    assert "Use the earlier summary instead" in page
    assert "The first summary." in page
    assert "The earlier summary is kept until you mark the note reviewed" in page.replace("\n", " ")


def test_when_writing_it_again_fails_the_summary_from_before_is_still_offered(admin):
    client, _, store = admin
    store.views["note-summary"].update(
        proposed=None, failed=True, earlier=EARLIER, earlier_job="e" * 64
    )
    store.views["lifecycle"]["suggestions"] = []
    page = client.get(REVIEW).text
    assert "The summary could not be written again. The one from before is still here." in page
    assert "The first summary." in page
    assert "Add the summary from before to the note" in page
    # Marking the note reviewed adds it, through the review it was written for.
    store.answers["note-check"] = {"filed": True, "path": "reference/2026/2026-11/Vendor call.md"}
    form = answers(summary="earlier")
    for name in ("s_0", "s_1", "s_2"):
        del form[name]
    post(client, REVIEW, form, page=REVIEW)
    (request,) = completed(store)
    assert request["meeting_summary"] == EARLIER
    assert request["job_id"] == "e" * 64


def test_while_a_new_summary_is_being_written_the_one_from_before_can_still_be_used(admin):
    client, _, store = admin
    store.views["note-summary"].update(
        proposed=None, writing=True, earlier=EARLIER, earlier_job="e" * 64
    )
    page = client.get(REVIEW).text
    assert "The first summary." in page
    assert "it is the one that is used unless you" in page.replace("\n", " ")
    assert 'value="earlier" checked' in page


def test_a_review_is_not_saved_over_an_edit_made_somewhere_else(admin):
    client, _, store = admin
    page = post(client, REVIEW, answers(if_match="sha256:" + "0" * 64, tags="mine"), page=REVIEW)
    said = follow(client, page)
    assert "was changed somewhere else while the page was open, so nothing was saved" in said
    assert completed(store) == []
    assert store.patches == []


def test_replace_is_held_to_the_file_that_was_shown(admin):
    client, _, store = admin
    taken: dict[str, Any] = {"name": "2026-11-11 Budget", "path": "work/b.md", "folder": "work"}
    taken |= {"edited": "2026-11-12T10:00:00+00:00", "words": 24, "hash": "sha256:seen"}
    mine = taken | {"path": "Review/b.md", "folder": "Review", "hash": "sha256:mine"}
    store.views["note"].update(
        stuck="notfiled", name_taken=True, taken=taken, mine=mine, both_as="b (2)", reviewed=True
    )
    asking = client.get(f"/admin/notes/{NOTE}?ask=replace").text
    assert 'name="seen" value="sha256:seen"' in asking
    store.answers["note-replace"] = {
        "refused": {"field": "seen", "message": "It was changed after you opened this page."}
    }
    page = follow(
        client,
        post(
            client, f"/admin/notes/{NOTE}/replace", {"seen": "sha256:seen"}, f"/admin/notes/{NOTE}"
        ),
    )
    assert store.asked("note-replace") == [{"id": NOTE, "seen": "sha256:seen"}]
    assert "It was changed after you opened this page." in page


def test_a_reviewed_note_is_refused_in_words(admin):
    client, _, store = admin
    store.answers["note-summary-again"] = {
        "refused": {"field": "note", "message": "This note has already been reviewed."}
    }
    page = follow(client, post(client, f"/admin/notes/{NOTE}/summary-again", page=REVIEW))
    assert "This note has already been reviewed." in page


def test_a_stopped_service_offers_test_now_and_edit_instead_of_the_button(admin):
    client, _, store = admin
    service = store.views["note-summary"]["service"]
    store.views["note-summary"]["state"] = "failing"
    page = client.get(REVIEW).text
    assert f"{service['name']} is not answering" in page
    assert "Test now" in page
    assert "Edit this service" in page
    assert f"/admin/services/{service['id']}/test" in page
    assert "Write the summary again" not in page


# -- reviewing a note in Admin --------------------------------------------------------------


def answers(**changes):
    form = {"how": "file", "date": "2026-11-12", "time": "11:22", "type": "reference"}
    form |= {"context": "internal", "account": "", "tags": "", "meeting": "none"}
    form |= {"s_0": "no", "s_1": "no", "s_2": "no", "summary": "new"}
    form["if_match"] = VIEWS["lifecycle"]["note"]["content_hash"]
    return form | changes


def completed(store):
    return [payload for name, payload in store.context_calls if name == "complete-review"]


def test_review_asks_for_an_answer_to_everything_it_was_unsure_about(admin):
    client, _, store = admin
    form = answers()
    del form["meeting"], form["s_1"]
    refused = post(client, REVIEW, form, page=REVIEW)
    assert refused.status_code == 422
    assert refused.text.count("Choose one, so Coppermind is not guessing.") == 2
    assert completed(store) == []


def test_the_chosen_summary_goes_with_the_review_and_nothing_else_is_written(admin):
    client, _, store = admin
    store.answers["note-check"] = {"filed": True, "path": "reference/2026/2026-11/Vendor call.md"}
    page = follow(client, post(client, REVIEW, answers(), page=REVIEW))
    (request,) = completed(store)
    assert request["meeting_summary"] == store.views["note-summary"]["proposed"]
    assert request["selected"] == []
    # Empty boxes are not properties: the note is not edited just for being reviewed.
    assert store.patches == []
    assert store.asked("note-check") == [{"id": NOTE, "reviewed": True}]
    assert "Reviewed and filed." in page


def test_the_earlier_summary_can_be_the_one_that_is_used(admin):
    client, _, store = admin
    store.views["note-summary"]["earlier"] = EARLIER
    store.answers["note-check"] = {"filed": True, "path": "reference/2026/2026-11/Vendor call.md"}
    post(client, REVIEW, answers(summary="earlier"), page=REVIEW)
    assert completed(store)[0]["meeting_summary"] == EARLIER


def test_leaving_the_summary_out_sends_none(admin):
    client, _, store = admin
    store.answers["note-check"] = {"filed": True, "path": "reference/2026/2026-11/Vendor call.md"}
    post(client, REVIEW, answers(summary="none"), page=REVIEW)
    assert "meeting_summary" not in completed(store)[0]


def test_what_was_accepted_is_taken_before_the_note_is_changed(admin):
    client, _, store = admin
    order: list[str] = []
    store.answers["note-check"] = {"filed": False, "settling": True, "error": "waiting"}

    async def complete(action, payload):
        order.append(action)
        store.context_calls.append((action, payload))
        return dict(store.views["lifecycle"]) if action == "lifecycle" else {}

    async def patch(identity, change, *, if_match):
        order.append("patch")
        store.patches.append((identity, change))

    store.context_action = complete
    store.patch_frontmatter = patch
    form = answers(s_0="yes", tags="rollout", meeting=MEETINGS[0])
    page = follow(client, post(client, REVIEW, form, page=REVIEW))
    writes = [step for step in order if step != "lifecycle"]
    assert writes == ["complete-review", "patch", "choose-recording-meeting"]
    assert completed(store)[0]["selected"] == [0]
    assert store.patches[0][1].set == {"tags": ["rollout"]}
    assert "is filed within a minute" in page


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


def test_a_work_note_with_no_time_can_be_said_to_have_none(admin):
    client, _, store = admin
    store.views["note"].update(stuck="notfiled", why="time", reason="It has no meeting time.")
    store.views["note"]["reviewed"] = True
    page = client.get(f"/admin/notes/{NOTE}").text
    assert "There was no set time" in page
    assert "Details for support" in page
    store.answers["note-no-time"] = {"filed": False, "settling": True, "error": "waiting"}
    page = follow(client, post(client, f"/admin/notes/{NOTE}/no-time", page=f"/admin/notes/{NOTE}"))
    assert store.asked("note-no-time") == [{"id": NOTE}]
    assert "Nothing is wrong." in page


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
    store.answers["note-gone"] = {"title": "Vendor call"}
    page = follow(client, post(client, f"/admin/notes/{NOTE}/gone", page=f"/admin/notes/{NOTE}"))
    assert "is no longer listed as a problem. Nothing was deleted by this." in page
    store.views["note"].update(recent=True)
    assert "No longer in your vault" in client.get(f"/admin/notes/{NOTE}").text


# -- a feature whose AI service is gone -------------------------------------------------------


def test_a_feature_whose_service_was_removed_outside_admin_says_so_on_its_page_and_on_home(admin):
    client, _, store = admin
    for name, view in (("sugg", "suggestions"), ("trans", "transcription")):
        store.views[view].update(service=None, state="failing")
        store.views["overview"][name].update(service=None, state="failing")
    for path, words in (
        ("/admin/suggestions", "Suggestions have stopped"),
        ("/admin/transcription", "Transcription has stopped"),
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert words in response.text
        assert "no longer in your list" in response.text
        assert "Use a different service" in response.text
    home = client.get("/admin")
    assert home.status_code == 200
    assert "Suggestions have stopped" in home.text
    assert "Transcription has stopped" in home.text
    assert "/admin/suggestions?change=service" in home.text


# -- adding the companion -----------------------------------------------------------------------


def place_companion(store):
    store.views["overview"]["companion"].update(placed=True, paired=[])
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
    assert made.status_code == 201
    control = StateStore(wiring.state_dir)
    (record,) = [key for key in load_keys(control).keys if key.name == "Home automation"]
    credential = made.text.split('id="newkey">')[1].split("<")[0]
    assert credential.startswith(f"cm_{record.key_id}_")
    assert (
        "This is the only\ntime the key is shown." in made.text or "only time the key" in made.text
    )
    for path in ("/admin/apps", "/admin/apps/new", f"/admin/apps/{record.key_id}"):
        assert credential not in client.get(path).text
    assert credential not in control.path_for("keys").read_text(encoding="utf-8")


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
