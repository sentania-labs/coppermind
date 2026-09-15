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
    logged_out = client.post("/v1/admin/logout", follow_redirects=False)
    assert logged_out.status_code == 303
    assert logged_out.headers["location"] == "/admin/claim"


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


def test_a_second_claim_is_refused_on_the_login_page_that_says_why(fresh):
    client, _, _ = fresh
    claim(client)
    refused = claim(client)
    assert refused.headers["location"] == "/admin/login?error=already_claimed"
    assert "Admin has already been claimed." in client.get(refused.headers["location"]).text


def test_rendered_forms_drive_claim_login_and_logout(fresh):
    client, _, sessions = fresh
    assert claim(client).headers["location"] == "/admin/login"

    logged_in = login(client)
    assert logged_in.status_code == 303
    assert logged_in.headers["location"] == "/admin?signed_in=1"
    assert client.cookies.get(COOKIE) == "test-session-token"
    assert "HttpOnly" in logged_in.headers["set-cookie"]
    assert "You are signed in" in client.get("/admin").text

    logged_out = client.post("/v1/admin/logout", data={}, follow_redirects=False)
    assert logged_out.status_code == 303
    assert logged_out.headers["location"] == "/admin/login"
    assert not sessions.tokens
    assert client.get("/admin").history[0].headers["location"] == "/admin/login"


def test_the_signed_in_marker_is_spent_once_and_an_ended_session_says_so(fresh):
    """The marker must not outlive the redirect that carried it."""
    client, _, sessions = fresh
    claim(client)
    logged_in = login(client)

    landed = client.get(logged_in.headers["location"], follow_redirects=False)
    assert landed.status_code == 303
    assert landed.headers["location"] == "/admin"
    assert "You are signed in" in client.get("/admin").text

    # The cookie outlives the row, so an ordinary expiry is reported as one.
    sessions.tokens.clear()
    ended = client.get("/admin", follow_redirects=False)
    assert ended.headers["location"] == "/admin/login?error=session_expired"
    assert "That session has ended." in client.get(ended.headers["location"]).text


def test_a_logout_without_a_live_session_returns_to_the_login_page(fresh):
    """The session lapses in an open tab, and Log out is the next thing clicked."""
    client, _, sessions = fresh
    claim(client)
    login(client)
    sessions.tokens.clear()

    stale = client.post("/v1/admin/logout", data={}, follow_redirects=False)
    assert stale.status_code == 303
    assert stale.headers["location"] == "/admin/login?error=session_expired"
    assert "That session has ended." in client.get(stale.headers["location"]).text


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


def test_the_documented_loopback_address_keeps_the_secure_cookie(tmp_path: Path):
    client, _, _ = _client(tmp_path, base_url="http://127.0.0.1:8082")
    with client:
        claim(client)
        logged_in = login(client)
        assert logged_in.headers["location"] == "/admin?signed_in=1"
        assert "Secure" in logged_in.headers["set-cookie"]


def test_an_address_admin_cannot_judge_still_gets_its_session(tmp_path: Path):
    """What a TLS-terminating proxy looks like from here: a plain http hop."""
    client, _, sessions = _client(tmp_path, base_url="http://coppermind.example:8082")
    with client:
        claim(client)
        logged_in = login(client)
        assert logged_in.headers["location"] == "/admin?signed_in=1"
        assert sessions.tokens


def test_a_browser_that_drops_the_session_cookie_is_told_why(tmp_path: Path):
    """A real browser on plain http discards the Secure cookie it was sent."""
    client, _, _ = _client(tmp_path, base_url="http://coppermind.example:8082")
    with client:
        claim(client)
        landed = client.post("/v1/admin/login", data={"password": PASSWORD})
        assert landed.status_code == 200
        assert str(landed.url).endswith("/admin/login?error=cookie_not_kept")
        assert "did not keep the session cookie" in landed.text
        assert "admin.cookie_secure" in landed.text
        assert "That session has ended." not in landed.text


def test_the_session_cookie_lasts_the_browser_session_not_the_row(fresh):
    """Set-Cookie is the contract: no Max-Age or Expires means until close."""
    client, _, _ = fresh
    claim(client)
    issued = login(client).headers["set-cookie"].lower()
    assert "max-age" not in issued
    assert "expires" not in issued


def test_a_settings_file_admin_cannot_read_names_the_file_and_the_key(fresh):
    """The docs send the operator to hand-edit settings.yaml, so it can be wrong."""
    client, wiring, sessions = fresh
    claim(client)
    settings_file = wiring.state_dir / "settings.yaml"

    settings_file.write_text(
        settings_file.read_text(encoding="utf-8").replace("session_hours: 12", "session_hours: 0"),
        encoding="utf-8",
    )
    refused = login(client)
    assert refused.status_code == 500
    assert str(settings_file) in refused.text
    assert "admin.session_hours" in refused.text
    assert "greater than or equal to 1" in refused.text
    assert not sessions.tokens

    settings_file.write_text("schema_version: 1\nrevision: 1\nadmin:\n  bogus: 1\n", "utf-8")
    mistyped = login(client)
    assert mistyped.status_code == 500
    assert "admin.bogus" in mistyped.text
    assert "Extra inputs are not permitted" in mistyped.text

    settings_file.write_text("admin:\n  session_hours: 12\n   cookie_secure: false\n", "utf-8")
    mangled = login(client)
    assert mangled.status_code == 500
    assert str(settings_file) in mangled.text
    assert "line 3" in mangled.text

    settings_file.unlink()
    missing = login(client)
    assert missing.status_code == 500
    assert str(settings_file) in missing.text


def test_a_session_database_outage_answers_503_rather_than_failing(fresh):
    client, _, sessions = fresh
    claim(client)
    login(client)

    sessions.available = False
    refused_login = login(client)
    assert refused_login.status_code == 503
    assert "session database could not be reached" in refused_login.text

    protected = client.get("/admin", follow_redirects=False)
    assert protected.status_code == 503
    assert "session database could not be reached" in protected.text
    assert protected.headers["content-type"].startswith("text/html")

    assert client.get("/readyz").status_code == 503
    assert client.get("/healthz").status_code == 200
