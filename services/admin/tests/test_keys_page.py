"""Drive key lifecycle through rendered forms and the API authenticator."""

import re
from html.parser import HTMLParser

import pytest
from coppermind_api.auth import ApiKeyAuthenticator
from coppermind_api.main import create_app as create_api
from coppermind_store.control import ControlState
from coppermind_store.notes import LocalStore
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from coppermind.api_keys import load_keys, split_credential
from coppermind.statefiles import StateStore
from coppermind.store_protocol import NoteSummary, Page
from services.admin.tests.browser import admin_browser as admin_browser
from services.admin.tests.test_admin import _client, claim


class Inputs(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.values = {}
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and "name" in attrs and attrs.get("type") != "checkbox":
            self.values[attrs["name"]] = attrs.get("value", "")


def create(client, **overrides):
    form = Inputs(client.get("/admin/keys").text).values
    form.update({"name": "automation", "scope:notes:read": "on"})
    form.update(overrides)
    return client.post("/v1/admin/keys/create", data=form)


def test_list_create_reveal_once_revoke(signed_in):
    client, wiring = signed_in
    created = create(client, name='<script>alert("name")</script>')
    assert created.status_code == 201
    match = re.search(r'<pre id="credential">([^<]+)</pre>', created.text)
    assert match is not None
    credential = match[1]
    assert created.headers["cache-control"] == "no-store"
    parsed = split_credential(credential)
    assert parsed is not None
    key_id, secret = parsed
    store = StateStore(wiring.state_dir)
    record = load_keys(store).keys[0]
    assert record.scopes == ["notes:read"]
    assert secret not in store.path_for("keys").read_text()
    second = client.get("/admin/keys")
    assert credential not in second.text and secret not in second.text
    assert record.hash not in second.text
    assert "<script>" not in second.text
    for value in [key_id, "notes:read", "Created:", "Revoked: No"]:
        assert value in second.text
    revision = load_keys(store).revision
    confirm = client.post(f"/v1/admin/keys/{key_id}/confirm", data=Inputs(second.text).values)
    assert "Confirm revocation" in confirm.text
    assert load_keys(store).keys[0].revoked_at is None
    revoked = client.post(f"/v1/admin/keys/{key_id}/revoke", data=Inputs(confirm.text).values)
    assert revoked.status_code == 200
    assert "Key revoked" in revoked.text
    assert load_keys(store).keys[0].revoked_at is not None
    assert load_keys(store).revision == revision + 1


def test_key_created_in_admin_authenticates_at_api(signed_in):
    client, wiring = signed_in
    match = re.search(r'<pre id="credential">([^<]+)</pre>', create(client).text)
    assert match is not None
    credential = match[1]

    # Authentication uses the real LocalStore and the Admin-written file.
    # Only note listing is a stand-in for the unavailable PostgreSQL mirror.
    class NotesReader:
        async def list_notes(self, query):
            return Page[NoteSummary](items=[], next_cursor=None)

    store = LocalStore(
        wiring.notes_dir, ControlState(wiring.state_dir), async_sessionmaker(), wiring.sources_dir
    )
    token = wiring.state_dir / "test-internal-token"
    token.write_text("test-internal-token")
    app = create_api(wiring.model_copy(update={"internal_token_file": token}))
    with TestClient(app) as api:
        app.state.store = NotesReader()
        app.state.api_key_auth._store = store
        response = api.post(
            "/v1/notes",
            headers={"Authorization": f"Bearer {credential}"},
            json={"title": "scope test"},
        )
        assert response.status_code == 403
        headers = {"Authorization": f"Bearer {credential}"}
        assert api.get("/v1/notes", headers=headers).status_code == 200
        key_id = load_keys(StateStore(wiring.state_dir)).keys[0].key_id
        form = Inputs(client.get("/admin/keys").text).values
        confirmation = client.post(f"/v1/admin/keys/{key_id}/confirm", data=form)
        assert (
            client.post(
                f"/v1/admin/keys/{key_id}/revoke", data=Inputs(confirmation.text).values
            ).status_code
            == 200
        )
        # A fresh cache has the same effect as the five-minute cache expiring.
        app.state.api_key_auth = ApiKeyAuthenticator(store)
        assert api.get("/v1/notes", headers=headers).status_code == 401


def test_empty_scopes_and_stale_key_forms(signed_in):
    client, wiring = signed_in
    form = Inputs(client.get("/admin/keys").text).values | {"name": "no permissions"}
    assert client.post("/v1/admin/keys/create", data=form).status_code == 201
    assert load_keys(StateStore(wiring.state_dir)).keys[0].scopes == []
    stale = client.post("/v1/admin/keys/create", data=form)
    assert stale.status_code == 409
    assert "revision" in stale.text
    key_id = load_keys(StateStore(wiring.state_dir)).keys[0].key_id
    assert (
        client.post(f"/v1/admin/keys/{key_id}/revoke", data=form | {"confirmed": "yes"}).status_code
        == 409
    )


@pytest.mark.parametrize("path", ["/admin/keys", "/admin/settings"])
def test_unauthenticated_page_redirects_to_login(tmp_path, path):
    client, _, _ = _client(tmp_path)
    with client:
        claim(client)
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"


@pytest.mark.parametrize(
    "path",
    [
        "/v1/admin/keys/create",
        "/v1/admin/keys/0123456789abcdef/confirm",
        "/v1/admin/keys/0123456789abcdef/revoke",
        "/v1/admin/settings",
        "/v1/admin/logout",
    ],
)
def test_csrf_missing_is_refused(signed_in, path):
    client, _ = signed_in
    response = client.post(path, data={"name": "no csrf"})
    assert response.status_code == 403
    assert "CSRF" in response.text


def test_claim_and_login_share_csrf_protection(tmp_path):
    client, _, _ = _client(tmp_path)
    with client:
        assert client.post("/v1/admin/claim", data={}).status_code == 403
        claim(client)
        assert client.post("/v1/admin/login", data={}).status_code == 403


def test_scope_choices_and_invalid_key_forms(signed_in):
    from coppermind.api_keys import API_SCOPES

    client, wiring = signed_in
    page = client.get("/admin/keys")
    for scope in API_SCOPES:
        assert f'name="scope:{scope}"' in page.text
    form = Inputs(page.text).values | {
        "name": "two scopes",
        "scope:notes:read": "on",
        "scope:notes:write": "on",
    }
    assert client.post("/v1/admin/keys/create", data=form).status_code == 201
    replay = client.post("/v1/admin/keys/create", data=form)
    assert replay.status_code == 409
    assert 'id="credential"' not in replay.text
    record = load_keys(StateStore(wiring.state_dir)).keys[0]
    assert set(record.scopes) == {"notes:read", "notes:write"}
    current = Inputs(client.get("/admin/keys").text).values
    assert client.post(f"/v1/admin/keys/{record.key_id}/revoke", data=current).status_code == 422
    assert load_keys(StateStore(wiring.state_dir)).keys[0].revoked_at is None
    assert create(client, **{"scope:invented": "on"}).status_code == 422
    assert len(load_keys(StateStore(wiring.state_dir)).keys) == 1
