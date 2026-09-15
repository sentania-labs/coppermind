from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from coppermind_admin.auth import SessionsUnavailable, token_hash
from coppermind_admin.main import COOKIE, create_app
from fastapi.testclient import TestClient

from coppermind.settings import Wiring, default_settings
from coppermind.statefiles import StateStore

PASSWORD = "correct horse battery staple"
CLAIM_CODE = "test-claim-code"


class MemorySessions:
    def __init__(self) -> None:
        self.tokens: set[str] = set()
        self.available = True

    def _check(self) -> None:
        if not self.available:
            raise SessionsUnavailable

    async def create(self, lifetime: timedelta) -> str:
        self._check()
        assert lifetime == timedelta(hours=12)
        token = "test-session-token"
        self.tokens.add(token_hash(token))
        return token

    async def valid(self, token: str) -> bool:
        self._check()
        return token_hash(token) in self.tokens

    async def delete(self, token: str) -> None:
        self._check()
        self.tokens.discard(token_hash(token))

    async def ready(self) -> bool:
        return self.available


def _client(tmp_path: Path, base_url: str = "https://testserver", **overrides: object):
    wiring = Wiring(data_dir=tmp_path / "data")
    settings = default_settings().model_dump(mode="json")
    settings["admin"].update(overrides)
    StateStore(wiring.state_dir).ensure("settings", settings)
    claim_code = wiring.state_dir / "internal" / "claim-code"
    claim_code.parent.mkdir(parents=True)
    claim_code.write_text(CLAIM_CODE + "\n", encoding="utf-8")
    sessions = MemorySessions()
    return TestClient(create_app(wiring, sessions), base_url=base_url), wiring, sessions


@pytest.fixture
def fresh(tmp_path: Path):
    client, wiring, sessions = _client(tmp_path)
    with client:
        yield client, wiring, sessions


def claim(client: TestClient, code: str = CLAIM_CODE, password: str = PASSWORD):
    return client.post(
        "/v1/admin/claim", data={"code": code, "password": password}, follow_redirects=False
    )


def login(client: TestClient, password: str = PASSWORD):
    return client.post("/v1/admin/login", data={"password": password}, follow_redirects=False)


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
    refused = claim(client, code="wrong")
    assert refused.headers["location"] == "/admin/claim?error=invalid_claim_code"
    assert not (wiring.state_dir / "admin.json").exists()
    assert "That claim code was not accepted." in client.get(refused.headers["location"]).text

    assert claim(client).headers["location"] == "/admin/login"
    contents = (wiring.state_dir / "admin.json").read_text(encoding="utf-8")
    assert PASSWORD not in contents
    assert '"password_hash": "$argon2' in contents
    assert not (wiring.state_dir / "internal" / "claim-code").exists()
    assert (wiring.state_dir / "admin.json").stat().st_mode & 0o077 == 0


def test_a_pasted_claim_code_is_accepted_around_its_whitespace(fresh):
    client, wiring, _ = fresh
    pasted = f"  {CLAIM_CODE} \n"
    assert claim(client, code=pasted).headers["location"] == "/admin/login"
    assert (wiring.state_dir / "admin.json").is_file()


def test_a_claim_code_carrying_a_non_ascii_character_is_refused_not_an_error(fresh):
    client, wiring, _ = fresh
    refused = claim(client, code=f"{CLAIM_CODE}’")
    assert refused.status_code == 303
    assert refused.headers["location"] == "/admin/claim?error=invalid_claim_code"
    assert not (wiring.state_dir / "admin.json").exists()


def test_a_short_password_is_reported_as_a_password_problem(fresh):
    client, wiring, _ = fresh
    refused = claim(client, password="eleven char")
    assert refused.headers["location"] == "/admin/claim?error=validation_error"
    assert not (wiring.state_dir / "admin.json").exists()
    page = client.get(refused.headers["location"]).text
    assert "password of at least 12 characters" in page
    assert "That claim code was not accepted." not in page


def test_a_second_claim_is_refused(fresh):
    client, _, _ = fresh
    claim(client)
    assert claim(client).headers["location"] == "/admin/claim?error=already_claimed"


def test_rendered_forms_drive_claim_login_and_logout(fresh):
    client, _, sessions = fresh
    assert claim(client).headers["location"] == "/admin/login"

    logged_in = login(client)
    assert logged_in.status_code == 303
    assert logged_in.headers["location"] == "/admin"
    assert client.cookies.get(COOKIE) == "test-session-token"
    assert "HttpOnly" in logged_in.headers["set-cookie"]
    assert "You are signed in" in client.get("/admin").text

    logged_out = client.post("/v1/admin/logout", data={}, follow_redirects=False)
    assert logged_out.status_code == 303
    assert logged_out.headers["location"] == "/admin/login"
    assert not sessions.tokens
    assert client.get("/admin").history[0].headers["location"] == "/admin/login"


def test_a_refused_password_returns_to_login_saying_so(fresh):
    client, _, _ = fresh
    claim(client)
    refused = login(client, password="wrong")
    assert refused.headers["location"] == "/admin/login?error=unauthorized"
    assert "That password was not accepted." in client.get(refused.headers["location"]).text
    assert COOKIE not in client.cookies


def test_the_session_cookie_is_secure_by_default_and_optional(tmp_path: Path):
    client, _, _ = _client(tmp_path)
    with client:
        claim(client)
        assert "Secure" in login(client).headers["set-cookie"]
        assert "You are signed in" in client.get("/admin").text

    plaintext, _, _ = _client(
        tmp_path / "plaintext", base_url="http://testserver", cookie_secure=False
    )
    with plaintext:
        claim(plaintext)
        assert "Secure" not in login(plaintext).headers["set-cookie"]
        assert "You are signed in" in plaintext.get("/admin").text


def test_a_session_database_outage_answers_503_rather_than_failing(fresh):
    client, _, sessions = fresh
    claim(client)
    login(client)

    sessions.available = False
    refused_login = login(client)
    assert refused_login.status_code == 503
    assert refused_login.json()["error"] == "sessions_unavailable"

    protected = client.get("/admin", follow_redirects=False)
    assert protected.status_code == 503
    assert protected.json()["error"] == "sessions_unavailable"

    assert client.get("/readyz").status_code == 503
    assert client.get("/healthz").status_code == 200
