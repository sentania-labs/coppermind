"""Obsidian Sync in Admin: signing in, connecting, and what syncs, against a stand-in helper.

The stand-in answers the way the sync helper's control endpoint does (its documented
JSON over HTTP), so the page is driven end to end without Obsidian.
"""

from __future__ import annotations

import io
import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import unquote

import pytest

from coppermind.statefiles import StateStore
from services.admin.tests.harness import admin as admin
from services.admin.tests.harness import follow, post

EMAIL = "operator@example.invalid"
CONFIGS = [
    "app",
    "appearance",
    "appearance-data",
    "hotkey",
    "core-plugin",
    "core-plugin-data",
    "community-plugin",
    "community-plugin-data",
]


class Helper:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.status: dict[str, Any] = {"configured": False, "signed_in": False}
        self.refuse: dict[str, dict[str, Any]] = {}
        self.timeouts: dict[str, list[float | None]] = {}

    def asked(self, action: str) -> list[Any]:
        return [body for name, body in self.calls if name == action]

    def __call__(self, request: Any, timeout: float | None = None) -> io.BytesIO:
        action = request.full_url.rsplit("/", 1)[1]
        self.calls.append((action, json.loads(request.data) if request.data else None))
        self.timeouts.setdefault(action, []).append(timeout)
        if action in self.refuse:
            raise HTTPError(
                request.full_url,
                502,
                "refused",
                {},  # type: ignore[arg-type]
                io.BytesIO(json.dumps(self.refuse[action]).encode()),
            )
        return io.BytesIO(json.dumps(self.status).encode())


@pytest.fixture
def helper(admin, monkeypatch):
    client, wiring, store = admin
    monkeypatch.setattr(type(wiring), "read_internal_token", lambda self: "token")
    store.views["overview"]["sync"].update(configured=False, state="not_connected")
    stand_in = Helper()
    monkeypatch.setattr("coppermind_admin.pages.sync.urlopen", stand_in)
    return stand_in


def test_a_refused_sign_in_sends_the_owner_back_without_their_address_in_the_url(admin, helper):
    client, _, _ = admin
    helper.refuse["login"] = {"error": "login_failed", "mfa_needed": False}
    response = post(
        client,
        "/admin/sync/signin",
        {"email": EMAIL, "password": "wrong"},
        page="/admin/sync",
    )
    assert response.status_code == 303
    location = unquote(response.headers["location"])
    assert location.startswith("/admin/sync?")
    assert "operator" not in location and "example.invalid" not in location
    page = client.get(response.headers["location"]).text
    assert "Obsidian did not accept that email and password" in page


def test_the_sign_in_code_request_does_not_put_the_address_in_the_url_either(admin, helper):
    client, _, _ = admin
    helper.refuse["login"] = {"error": "login_failed", "mfa_needed": True}
    response = post(
        client,
        "/admin/sync/signin",
        {"email": EMAIL, "password": "right"},
        page="/admin/sync",
    )
    assert "operator" not in unquote(response.headers["location"])
    assert "sign-in code" in client.get(response.headers["location"]).text


def test_connecting_goes_sign_in_then_vault_then_name_and_ends_connected(admin, helper):
    client, _, _ = admin
    post(client, "/admin/sync/signin", {"email": EMAIL, "password": "pw"}, page="/admin/sync")
    assert helper.asked("login") == [{"email": EMAIL, "password": "pw", "mfa_code": ""}]

    helper.status = {
        "configured": False,
        "signed_in": True,
        "vaults": [{"id": "v1", "name": "My notes", "locked": False}],
    }
    page = client.get("/admin/sync").text
    assert "Which vault should Coppermind join?" in page and "My notes" in page
    post(client, "/admin/sync/vault", {"vault": "v1"}, page="/admin/sync")
    assert helper.asked("vault") == [{"vault_id": "v1", "password": ""}]

    helper.status = {**helper.status, "choice": {"vault_id": "v1", "vault_name": "My notes"}}
    assert "Ready to connect" in client.get("/admin/sync").text
    helper.status = {"configured": True, "vault_name": "My notes", "state": "syncing"}
    landed = follow(
        client,
        post(client, "/admin/sync/connect", {"device_name": "Kitchen"}, page="/admin/sync"),
    )
    assert helper.asked("connect") == [{"device_name": "Kitchen"}]
    assert 'Connected. Coppermind is now syncing with "My notes"' in landed


def test_a_failed_connect_says_what_to_do_and_keeps_the_helper_text_behind_support(admin, helper):
    client, _, _ = admin
    helper.status = {
        "configured": False,
        "signed_in": True,
        "choice": {"vault_id": "v1", "vault_name": "My notes"},
    }
    helper.refuse["connect"] = {"error": "connect_failed", "detail": "Obsidian said 0xDEAD"}
    landed = follow(
        client,
        post(client, "/admin/sync/connect", {"device_name": "Kitchen"}, page="/admin/sync"),
    )
    assert "could not connect to that vault, so nothing was changed" in landed
    assert landed.index("Details for support") < landed.index("Obsidian said 0xDEAD")
    before_details = landed[: landed.index("Details for support")]
    assert "0xDEAD" not in before_details


def saved_configs(wiring: Any) -> list[str]:
    return StateStore(wiring.state_dir).read("settings").body["sync"]["sync_configs"]


def test_your_own_obsidian_settings_is_off_until_chosen_and_never_forced(admin, helper):
    client, wiring, _ = admin
    helper.status = {"configured": True, "vault_name": "My notes", "state": "syncing"}
    post(client, "/admin/sync/what", {"image": "on", "conflict": "merge"}, page="/admin/sync/what")
    assert saved_configs(wiring) == []
    assert 'name="own" value="on" checked' not in client.get("/admin/sync/what").text

    post(
        client,
        "/admin/sync/what",
        {"image": "on", "own": "on", "conflict": "merge"},
        page="/admin/sync/what",
    )
    assert saved_configs(wiring) == CONFIGS
    assert 'name="own" value="on" checked' in client.get("/admin/sync/what").text

    post(client, "/admin/sync/what", {"image": "on", "conflict": "merge"}, page="/admin/sync/what")
    assert saved_configs(wiring) == []


CARRIES = (
    "Your own Obsidian settings sync too, which is what carries the Coppermind companion to "
    "your other devices."
)


def test_what_syncs_says_beside_the_tick_that_the_companion_needs_it(admin, helper):
    client, _, _ = admin
    helper.status = {"configured": True, "vault_name": "My notes", "state": "syncing"}
    page = client.get("/admin/sync/what").text
    tick = page[page.index('name="own"') :]
    tick = tick[: tick.index("</label>")]
    assert (
        "The Coppermind companion is an add-on, so it reaches your other devices only when "
        "this is ticked." in tick
    )
    # The page no longer tells the owner to leave off what the companion step asks for.
    assert "Most people leave this off" not in page


def test_the_saved_banner_and_the_sync_page_mention_the_tick_only_when_it_is_on(admin, helper):
    client, wiring, _ = admin
    helper.status = {"configured": True, "vault_name": "My notes", "state": "syncing"}
    sent = post(
        client,
        "/admin/sync/what",
        {"image": "on", "own": "on", "conflict": "merge"},
        page="/admin/sync/what",
    )
    landed = follow(client, sent)
    # Once in the banner that says what was saved, once in the What syncs line.
    assert landed.count(CARRIES) == 2
    assert client.get("/admin/sync").text.count(CARRIES) == 1

    sent = post(
        client, "/admin/sync/what", {"image": "on", "conflict": "merge"}, page="/admin/sync/what"
    )
    assert CARRIES not in follow(client, sent)
    assert saved_configs(wiring) == []
