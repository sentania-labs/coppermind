from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from coppermind_admin.auth import token_hash
from coppermind_admin.main import COOKIE, create_app
from fastapi.testclient import TestClient

from coppermind.settings import Wiring, default_settings
from coppermind.statefiles import StateStore

PASSWORD = "correct horse battery staple"
CLAIM_CODE = "test-claim-code"


class MemorySessions:
    def __init__(self) -> None:
        self.tokens: set[str] = set()

    async def create(self, lifetime: timedelta) -> str:
        assert lifetime == timedelta(hours=12)
        token = "test-session-token"
        self.tokens.add(token_hash(token))
        return token

    async def valid(self, token: str) -> bool:
        return token_hash(token) in self.tokens

    async def delete(self, token: str) -> None:
        self.tokens.discard(token_hash(token))

    async def ready(self) -> bool:
        return True


@pytest.fixture
def fresh(tmp_path: Path):
    wiring = Wiring(data_dir=tmp_path / "data")
    StateStore(wiring.state_dir).ensure("settings", default_settings().model_dump(mode="json"))
    claim_code = wiring.state_dir / "internal" / "claim-code"
    claim_code.parent.mkdir(parents=True)
    claim_code.write_text(CLAIM_CODE + "\n", encoding="utf-8")
    sessions = MemorySessions()
    with TestClient(create_app(wiring, sessions)) as client:
        yield client, wiring, sessions


def claim(client: TestClient) -> None:
    response = client.post("/v1/admin/claim", json={"code": CLAIM_CODE, "password": PASSWORD})
    assert response.status_code == 201


def test_fresh_admin_is_unclaimed_and_only_exposes_the_claim_and_login_pages(fresh):
    client, _, _ = fresh
    assert client.get("/admin/claim").status_code == 200
    assert client.get("/admin/login").history[0].headers["location"] == "/admin/claim"
    assert client.get("/admin").history[0].headers["location"] == "/admin/claim"
    response = client.post("/v1/admin/logout")
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_claim_needs_the_bootstrap_code_and_stores_only_an_argon2_hash(fresh):
    client, wiring, _ = fresh
    refused = client.post("/v1/admin/claim", json={"code": "wrong", "password": PASSWORD})
    assert refused.status_code == 403
    assert not (wiring.state_dir / "admin.json").exists()

    claim(client)
    contents = (wiring.state_dir / "admin.json").read_text(encoding="utf-8")
    assert PASSWORD not in contents
    assert '"password_hash": "$argon2' in contents
    assert not (wiring.state_dir / "internal" / "claim-code").exists()
    assert (wiring.state_dir / "admin.json").stat().st_mode & 0o077 == 0


def test_a_second_claim_is_refused(fresh):
    client, _, _ = fresh
    claim(client)
    response = client.post("/v1/admin/claim", json={"code": CLAIM_CODE, "password": PASSWORD})
    assert response.status_code == 409
    assert response.json()["error"] == "already_claimed"


def test_rendered_forms_drive_claim_login_and_logout(fresh):
    client, _, sessions = fresh
    claimed = client.post(
        "/v1/admin/claim",
        data={"code": CLAIM_CODE, "password": PASSWORD},
        follow_redirects=False,
    )
    assert claimed.status_code == 303
    assert claimed.headers["location"] == "/admin/login"

    logged_in = client.post("/v1/admin/login", data={"password": PASSWORD}, follow_redirects=False)
    assert logged_in.status_code == 303
    assert logged_in.headers["location"] == "/admin"
    assert "You are signed in" in client.get("/admin").text

    logged_out = client.post("/v1/admin/logout", data={"action": "logout"}, follow_redirects=False)
    assert logged_out.status_code == 303
    assert logged_out.headers["location"] == "/admin/login"
    assert not sessions.tokens


def test_login_reaches_the_protected_page_and_logout_revokes_the_session(fresh):
    client, _, sessions = fresh
    claim(client)
    assert client.post("/v1/admin/login", json={"password": "wrong"}).status_code == 401

    login = client.post("/v1/admin/login", json={"password": PASSWORD})
    assert login.status_code == 200
    assert login.cookies.get(COOKIE) == "test-session-token"
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "You are signed in" in client.get("/admin").text

    logout = client.post("/v1/admin/logout")
    assert logout.status_code == 204
    assert not sessions.tokens
    refused = client.get("/admin")
    assert refused.history[0].headers["location"] == "/admin/login"
