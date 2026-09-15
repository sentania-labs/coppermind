from __future__ import annotations

import json
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

    async def revoke_all(self) -> None:
        self._check()
        self.tokens.clear()

    async def ready(self) -> bool:
        return self.available


def _client(tmp_path: Path, base_url: str = "https://testserver"):
    wiring = Wiring(data_dir=tmp_path / "data")
    StateStore(wiring.state_dir).ensure("settings", default_settings().model_dump(mode="json"))
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
    assert logged_in.headers["location"] == "/admin"
    assert client.cookies.get(COOKIE) == "test-session-token"
    assert "HttpOnly" in logged_in.headers["set-cookie"]
    assert "You are signed in" in client.get("/admin").text

    logged_out = client.post("/v1/admin/logout", data={}, follow_redirects=False)
    assert logged_out.status_code == 303
    assert logged_out.headers["location"] == "/admin/login"
    assert not sessions.tokens
    assert client.get("/admin").history[0].headers["location"] == "/admin/login"


def test_an_ended_session_says_so_on_the_login_page(fresh):
    """The cookie outlives the row, so an ordinary expiry is reported as one."""
    client, _, sessions = fresh
    claim(client)
    login(client)
    assert "You are signed in" in client.get("/admin").text

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


def test_the_session_cookie_is_always_secure(tmp_path: Path):
    client, _, _ = _client(tmp_path)
    with client:
        claim(client)
        assert "Secure" in login(client).headers["set-cookie"]
        assert "You are signed in" in client.get("/admin").text


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

    settings_file.write_text("general:\n  timezone: UTC\n   reconcile: broken\n", "utf-8")
    mangled = login(client)
    assert mangled.status_code == 500
    assert str(settings_file) in mangled.text
    assert "line 3" in mangled.text

    settings_file.unlink()
    missing = login(client)
    assert missing.status_code == 500
    assert str(settings_file) in missing.text


def test_re_claiming_after_recovery_ends_every_session_the_old_password_opened(fresh):
    """The README recovery replaces the credential, so it must end its sessions."""
    client, wiring, sessions = fresh
    claim(client)
    login(client)
    assert "You are signed in" in client.get("/admin").text

    (wiring.state_dir / "admin.json").unlink()
    (wiring.state_dir / "internal" / "claim-code").write_text(CLAIM_CODE + "\n", encoding="utf-8")
    reclaimed = claim(client, password="a different admin password")
    assert reclaimed.headers["location"] == "/admin/login"

    assert not sessions.tokens
    refused = client.get("/admin", follow_redirects=False)
    assert refused.headers["location"] == "/admin/login?error=session_expired"


def test_a_stale_login_on_an_unclaimed_admin_goes_to_the_claim_page(fresh):
    """The operator removed admin.json to recover, then submitted an old tab."""
    client, wiring, _ = fresh
    claim(client)
    (wiring.state_dir / "admin.json").unlink()

    stale = login(client)
    assert stale.status_code == 303
    assert stale.headers["location"] == "/admin/claim"


def test_an_admin_record_admin_cannot_read_is_not_blamed_on_the_password(fresh):
    """A truncated or restored-in-part admin.json is not a wrong password."""
    client, wiring, sessions = fresh
    claim(client)
    record = wiring.state_dir / "admin.json"

    record.write_text('{"schema_version": 1, "password_h', encoding="utf-8")
    refused = login(client)
    assert refused.status_code == 500
    assert str(record) in refused.text
    assert "That password was not accepted." not in refused.text
    assert "claim Admin" in refused.text
    assert not sessions.tokens

    record.write_text('{"schema_version": 1, "password_hash": "not-a-hash"}', encoding="utf-8")
    unusable = login(client)
    assert unusable.status_code == 500
    assert str(record) in unusable.text

    for broken in (
        '{"password_hash": null}',
        '{"password_hash": 12}',
        '{"password_h' + 'ash": "$argon2\u2019"}',
    ):
        record.write_text(broken, encoding="utf-8")
        answered = login(client)
        assert answered.status_code == 500
        assert str(record) in answered.text


def test_a_damaged_argon2_hash_is_not_reported_as_a_wrong_password(fresh):
    """The header still parses, so only the body is gone. That is not the password."""
    client, wiring, _ = fresh
    claim(client)
    record = wiring.state_dir / "admin.json"

    intact = json.loads(record.read_text(encoding="utf-8"))
    intact["password_hash"] = intact["password_hash"][:-10]
    record.write_text(json.dumps(intact), encoding="utf-8")

    answered = login(client)
    assert answered.status_code == 500
    assert str(record) in answered.text
    assert "That password was not accepted." not in answered.text


def test_a_state_file_whose_revision_was_emptied_names_the_file(fresh):
    """Deleting a value and leaving its key is an ordinary hand-edit slip."""
    client, wiring, _ = fresh
    claim(client)
    settings_file = wiring.state_dir / "settings.yaml"

    settings_file.write_text(
        settings_file.read_text(encoding="utf-8").replace("revision: 1", "revision:"),
        encoding="utf-8",
    )
    answered = login(client)
    assert answered.status_code == 500
    assert str(settings_file) in answered.text
    assert "revision" in answered.text

    record = wiring.state_dir / "admin.json"
    blanked = json.loads(record.read_text(encoding="utf-8")) | {"revision": None}
    record.write_text(json.dumps(blanked), encoding="utf-8")
    named = login(client)
    assert named.status_code == 500
    assert str(record) in named.text


def test_a_recovery_claim_during_a_session_outage_stays_unclaimed_and_retryable(fresh):
    """Fail closed: no new credential while the old sessions cannot be ended."""
    client, wiring, sessions = fresh
    claim(client)
    login(client)
    record = wiring.state_dir / "admin.json"
    claim_code = wiring.state_dir / "internal" / "claim-code"

    record.unlink()
    claim_code.write_text(CLAIM_CODE + "\n", encoding="utf-8")
    sessions.available = False

    refused = claim(client, password="a different admin password")
    assert refused.status_code == 503
    assert not record.exists()
    assert claim_code.is_file()

    sessions.available = True
    assert claim(client, password="a different admin password").headers["location"] == (
        "/admin/login"
    )
    assert record.is_file()
    assert not sessions.tokens
    assert client.get("/admin", follow_redirects=False).headers["location"] == (
        "/admin/login?error=session_expired"
    )


def test_a_refused_claim_code_never_ends_a_live_session(fresh):
    """Sessions are only revoked once the code is accepted."""
    client, wiring, sessions = fresh
    claim(client)
    login(client)
    (wiring.state_dir / "admin.json").unlink()
    (wiring.state_dir / "internal" / "claim-code").write_text(CLAIM_CODE, encoding="utf-8")

    refused = claim(client, code="not the code")
    assert refused.headers["location"] == "/admin/claim?error=invalid_claim_code"
    assert sessions.tokens


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
