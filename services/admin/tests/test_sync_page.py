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
    client.post("/v1/admin/claim", data={"code": "test-claim", "password": "test-admin-password"})
    client.post("/v1/admin/login", data={"password": "test-admin-password"})


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
    response = client.post("/admin/sync/connect", data=FORM)
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
        response = client.post(f"/admin/sync/{action}", data={"submit": "yes"})
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
    response = client.post("/admin/sync/connect", data=FORM | change)
    assert "Check the required fields" in response.text
    assert_clean(response)
    assert calls == []


def test_helper_error_body_is_never_rendered(sync_client):
    client, _, calls, fail = sync_client
    sign_in(client)
    fail.append(True)
    response = client.post("/admin/sync/connect", data=FORM | {"existing_vault": "true"})
    assert "Sync request failed" in response.text
    assert_clean(response)
    assert calls[0][1]["existing_vault"] is True


def test_helper_device_default_matches_shared_settings():
    root = Path(__file__).resolve().parents[3]
    source = (root / "services/obsidian-sync/settings.mjs").read_text()
    assert f'DEFAULT_DEVICE_NAME = "{default_settings().sync.device_name}"' in source
