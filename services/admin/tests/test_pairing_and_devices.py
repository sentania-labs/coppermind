"""Pairing codes, the devices on the Sync page, and the people page's quoting."""

from __future__ import annotations

import re

from services.admin.tests.harness import admin as admin
from services.admin.tests.harness import post
from services.admin.tests.test_sync_page import helper as helper  # noqa: F401

LIVE = {"code": "123456", "done": None, "expires_at": "2099-01-01T00:00:00+00:00"}


def fresh_requests(store):
    return [p for p in store.asked("pairing-start") if p.get("fresh")]


def test_looking_at_the_pairing_page_never_starts_a_new_code(admin):
    client, _, store = admin
    store.answers["pairing-state"] = LIVE
    for path in ("/admin/apps/pair", "/admin/apps/pair?new=1", "/admin/apps/pair?from=sync&new=1"):
        assert client.get(path).status_code == 200
    assert fresh_requests(store) == []


def test_a_locked_out_code_is_not_replaced_by_looking_at_the_pairing_page(admin):
    client, _, store = admin
    store.answers["pairing-state"] = {
        "code": "",
        "done": None,
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    for _ in range(3):
        page = client.get("/admin/apps/pair").text
    assert "too many wrong codes" in page
    assert 'action="/admin/apps/pair/code"' in page and "Make a new code" in page
    assert store.asked("pairing-start") == []


def test_the_first_visit_starts_a_code_when_none_was_ever_made(admin):
    client, _, store = admin
    store.answers["pairing-state"] = {"code": "", "done": None}
    store.answers["pairing-start"] = {
        "code": "654321",
        "done": None,
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    page = client.get("/admin/apps/pair").text
    assert "654 321" in page or "654321" in page
    assert len(store.asked("pairing-start")) == 1


def test_a_new_code_is_asked_for_by_pressing_the_button_with_the_page_token(admin):
    client, _, store = admin
    store.answers["pairing-state"] = LIVE
    page = client.get("/admin/apps/pair?from=sync").text
    assert 'action="/admin/apps/pair/code"' in page
    pressed = post(client, "/admin/apps/pair/code", {"from": "sync"}, page="/admin/apps/pair")
    assert pressed.status_code == 303
    assert pressed.headers["location"] == "/admin/apps/pair?from=sync"
    assert len(fresh_requests(store)) == 1
    refused = client.post("/admin/apps/pair/code", data={"from": "sync"})
    assert refused.status_code == 403
    assert len(fresh_requests(store)) == 1


def test_choosing_the_companion_when_connecting_an_app_makes_a_code_by_post_only(admin):
    client, _, store = admin
    store.answers["pairing-state"] = LIVE
    assert "pairing-start" not in [name for name, _ in store.calls]
    client.get("/admin/apps/new")
    assert fresh_requests(store) == []
    made = post(client, "/admin/apps/new", {"kind": "companion"}, page="/admin/apps/new")
    assert made.headers["location"] == "/admin/apps/pair"
    assert len(fresh_requests(store)) == 1


def test_the_never_save_question_names_a_person_with_an_apostrophe_once_escaped(admin):
    client, _, store = admin
    person = store.views["person"]
    person.update(name="O'Brien Smith", never=False, samples=[])
    asked = client.get(f"/admin/people/{person['id']}?ask=never").text
    dialog = asked.split('class="dialog"')[1]
    body = dialog.split('<div class="muted">')[1].split("</div>")[0]
    assert "&amp;" not in body
    assert re.search(r"learn or keep O(&#x27;|')Brien", body)


def two_companions_on_one_device(store):
    first = dict(store.views["overview"]["companion"]["paired"][0])
    second = {**first, "id": "9999999999999999", "name": "Obsidian companion on My iPhone (2)"}
    store.views["overview"]["companion"]["paired"] = [first, second]
    store.views["overview"]["sync"]["state"] = "syncing"
    return first, second


def test_every_companion_paired_under_one_device_name_has_its_own_row_and_unpair(admin, helper):
    client, _, store = admin
    first, second = two_companions_on_one_device(store)
    helper.status = {
        "configured": True,
        "vault_name": "My notes",
        "state": "syncing",
        "devices": [{"name": first["device"], "last_change_at": "2026-11-12T10:00:00+00:00"}],
    }
    page = client.get("/admin/sync").text
    assert page.count("Companion paired") == 2
    assert f"ask=unpair%3A{first['id']}" in page or f"ask=unpair:{first['id']}" in page
    assert f"ask=unpair%3A{second['id']}" in page or f"ask=unpair:{second['id']}" in page


def test_reading_the_sync_status_never_waits_on_the_helper_for_minutes(admin, helper):
    client, _, store = admin
    helper.status = {"configured": True, "vault_name": "My notes", "state": "syncing"}
    client.get("/admin/sync")
    assert helper.timeouts["status"]
    assert all(t is not None and t <= 10 for t in helper.timeouts["status"])
    post(
        client, "/admin/sync/signin", {"email": "a@b.example", "password": "pw"}, page="/admin/sync"
    )
    assert helper.timeouts["login"] == [300]
