from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from coppermind_admin.main import create_app
from fastapi.testclient import TestClient

from coppermind.settings import Wiring, default_settings, read_settings
from coppermind.statefiles import StateStore
from services.admin.tests.test_admin import form_token

FORM = {
    "email": "operator@example.invalid",
    "password": "test-account-password",
    "mfa_code": "123789",
    "encryption_password": "test-encryption-password",
    "encryption_confirm": "test-encryption-password",
    "vault_name": "Phone notes",
    "device_name": "Kitchen server",
    "existing_vault": "false",
    "plan": "plus",
}
SECRETS = [FORM[key] for key in ("email", "password", "mfa_code", "encryption_password")]


@pytest.fixture
def sync_client(tmp_path, monkeypatch):
    calls = []
    status = {"configured": False, "state": "not_connected"}
    fail: list[bool] = []

    class Helper(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            assert self.headers["Authorization"] == "Bearer helper-test-token"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(status).encode())

        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer helper-test-token"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            if fail:
                self.send_response(502)
                self.end_headers()
                self.wfile.write(json.dumps({"detail": " ".join(SECRETS)}).encode())
                return
            if self.path == "/connect":
                status.update(
                    configured=True,
                    state="syncing",
                    vault_name=body["vault_name"],
                    device_name=body["device_name"],
                )
            elif self.path == "/configure":
                status.update(
                    configured=True,
                    config_matches=True,
                    config_live=True,
                    client_status={"fileTypes": ["unsupported"], "excludedFolders": []},
                )
            elif self.path == "/pause":
                status["state"] = "paused"
            elif self.path == "/resume":
                status["state"] = "syncing"
            elif self.path == "/disconnect":
                status.update(configured=False, state="not_connected")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(status).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Helper)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("COPPERMIND_SYNC_URL", f"http://127.0.0.1:{server.server_port}")
    token = tmp_path / "internal-token"
    token.write_text("helper-test-token")
    wiring = Wiring(data_dir=tmp_path, internal_token_file=token)
    state = StateStore(wiring.state_dir)
    state.ensure("settings", default_settings().model_dump())
    claim = wiring.state_dir / "internal" / "claim-code"
    claim.parent.mkdir()
    claim.write_text("test-claim")
    with TestClient(create_app(wiring), base_url="https://testserver") as client:
        yield client, state, calls, fail
    server.shutdown()
    server.server_close()
    worker.join()


def sign_in(client):
    client.post(
        "/v1/admin/claim",
        data={
            "code": "test-claim",
            "password": "test-admin-password",
            "csrf": form_token(client, "/admin/claim"),
        },
    )
    client.post(
        "/v1/admin/login",
        data={"password": "test-admin-password", "csrf": form_token(client, "/admin/login")},
    )


def assert_clean(response):
    for secret in SECRETS:
        assert secret not in response.text
        assert secret not in str(response.headers)


def test_guided_connect_and_lifecycle(sync_client):
    client, state, calls, _ = sync_client
    assert client.get("/admin/sync", follow_redirects=False).status_code == 303
    sign_in(client)
    page = client.get("/admin/sync")
    assert 'value="coppermind-server"' in page.text
    assert 'value="standard" selected' in page.text
    response = client.post(
        "/admin/sync/connect", data=FORM | {"csrf": form_token(client, "/admin")}
    )
    assert response.status_code == 200
    assert "Phone notes" in response.text
    assert "Kitchen server" in response.text
    assert_clean(response)
    assert calls[0] == (
        "/connect",
        {
            key: value
            for key, value in FORM.items()
            if key not in {"plan", "encryption_confirm", "existing_vault"}
        }
        | {"existing_vault": False},
    )
    assert read_settings(state).sync.plan == "plus"
    assert read_settings(state).sync.device_name == "Kitchen server"
    for action, expected in [
        ("pause", "paused"),
        ("resume", "syncing"),
        ("disconnect", "not_connected"),
    ]:
        response = client.post(
            f"/admin/sync/{action}", data={"submit": "yes", "csrf": form_token(client, "/admin")}
        )
        assert expected in response.text
        assert_clean(response)
    for file in state.state_dir.rglob("*"):
        if file.is_file():
            assert all(secret not in file.read_text() for secret in SECRETS)


@pytest.mark.parametrize(
    "change",
    [
        {"encryption_confirm": "mismatch"},
        {"email": ""},
        {"plan": "invalid"},
        {"vault_name": FORM["password"]},
        {"device_name": FORM["email"]},
    ],
)
def test_validation_never_echoes_credentials(sync_client, change):
    client, _, calls, _ = sync_client
    sign_in(client)
    response = client.post(
        "/admin/sync/connect", data=FORM | change | {"csrf": form_token(client, "/admin")}
    )
    assert "Check the required fields" in response.text
    assert_clean(response)
    assert calls == []


def test_helper_error_body_is_never_rendered(sync_client):
    client, _, calls, fail = sync_client
    sign_in(client)
    fail.append(True)
    response = client.post(
        "/admin/sync/connect",
        data=FORM | {"existing_vault": "true", "csrf": form_token(client, "/admin")},
    )
    assert "Sync request failed" in response.text
    assert_clean(response)
    assert calls[0][1]["existing_vault"] is True


def test_helper_device_default_matches_shared_settings():
    root = Path(__file__).resolve().parents[3]
    source = (root / "services/obsidian-sync/settings.mjs").read_text()
    assert f'DEFAULT_DEVICE_NAME = "{default_settings().sync.device_name}"' in source


def preferences(client, state, **changes):
    return {
        "revision": str(state.read("settings").revision),
        "device_name": "coppermind-server",
        "mode": "bidirectional",
        "conflict_strategy": "merge",
        "plan": "plus",
        "excluded_folders": "_Trash",
        "max_file_bytes": "",
        "max_total_bytes": "",
        "preset": "all-files",
        "csrf": form_token(client, "/admin"),
    } | changes


def test_apply_all_files_live_display_and_stale_revision(sync_client):
    client, state, calls, _ = sync_client
    sign_in(client)
    form = preferences(client, state)
    response = client.post("/admin/sync/configure", data=form)
    assert "saved and verified" in response.text
    assert "Saved settings match" in response.text
    assert "unsupported" in response.text
    assert "unsupported" in read_settings(state).sync.file_types
    assert read_settings(state).sync.max_file_bytes is None
    assert calls[-1] == ("/configure", {})
    count = len(calls)
    response = client.post("/admin/sync/configure", data=form | {"device_name": "stale"})
    assert "Settings changed in another tab" in response.text
    assert len(calls) == count
    assert read_settings(state).sync.device_name == "coppermind-server"


def test_empty_selections_and_failed_apply_remain_pending(sync_client):
    client, state, _, fail = sync_client
    sign_in(client)
    fail.append(True)
    response = client.post(
        "/admin/sync/configure",
        data=preferences(client, state, preset="save", excluded_folders="", file_pdf="yes"),
    )
    assert "Application is pending" in response.text
    assert "saved and verified" not in response.text
    saved = read_settings(state).sync
    assert saved.file_types == ["pdf"]
    assert saved.excluded_folders == saved.sync_configs == []
    assert_clean(response)


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "invalid"},
        {"preset": "save"},
        {"mode": "mirror-remote"},
        {"excluded_folders": "../notes"},
        {"excluded_folders": "one,two"},
        {"max_file_bytes": "-1"},
        {"device_name": "bad\nname"},
    ],
)
def test_invalid_preferences_do_not_write_or_apply(sync_client, changes):
    client, state, calls, _ = sync_client
    sign_in(client)
    revision = state.read("settings").revision
    response = client.post("/admin/sync/configure", data=preferences(client, state, **changes))
    assert "Invalid sync preferences" in response.text
    assert state.read("settings").revision == revision
    assert calls == []


def test_live_values_are_escaped_and_unavailable_is_explicit():
    from coppermind_admin.pages.sync import configuration_form

    from coppermind.settings import ProductSettings

    page = configuration_form(
        ProductSettings(),
        1,
        {
            "configured": True,
            "config_live": True,
            "client_status": {"excludedFolders": ["<script>bad</script>"]},
        },
    )
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    page = configuration_form(ProductSettings(), 1, {"configured": True})
    assert "Live configuration unavailable" in page
