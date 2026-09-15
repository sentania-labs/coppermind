from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from coppermind_admin.auth import AdminCredentials, SignedSessions
from coppermind_admin.main import COOKIE, MAX_FORM_BYTES, create_app
from fastapi.testclient import TestClient

from coppermind.settings import Wiring, default_settings
from coppermind.statefiles import StateStore

PASSWORD = "correct horse battery staple"
CLAIM_CODE = "test-claim-code"


def _client(tmp_path: Path, base_url: str = "https://testserver"):
    wiring = Wiring(data_dir=tmp_path / "data")
    StateStore(wiring.state_dir).ensure("settings", default_settings().model_dump(mode="json"))
    claim_code = wiring.state_dir / "internal" / "claim-code"
    claim_code.parent.mkdir(parents=True)
    claim_code.write_text(CLAIM_CODE + "\n", encoding="utf-8")
    sessions = SignedSessions(AdminCredentials(wiring.state_dir))
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


def test_claim_needs_the_bootstrap_code_and_stores_the_session_secret_beside_the_hash(fresh):
    client, wiring, _ = fresh
    refused = claim(client, code="wrong")
    assert refused.headers["location"] == "/admin/claim?error=invalid_claim_code"
    assert not (wiring.state_dir / "admin.json").exists()
    assert "That claim code was not accepted." in client.get(refused.headers["location"]).text

    assert claim(client).headers["location"] == "/admin/login"
    record = wiring.state_dir / "admin.json"
    contents = record.read_text(encoding="utf-8")
    stored = json.loads(contents)
    assert PASSWORD not in contents
    assert stored["password_hash"].startswith("$argon2")
    assert len(bytes.fromhex(stored["session_secret"])) == 32
    assert not (wiring.state_dir / "internal" / "claim-code").exists()
    assert record.stat().st_mode & 0o077 == 0


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
    token = client.cookies.get(COOKIE)
    assert token is not None
    assert sessions.valid(token)
    assert "HttpOnly" in logged_in.headers["set-cookie"]
    assert "You are signed in" in client.get("/admin").text

    logged_out = client.post("/v1/admin/logout", data={}, follow_redirects=False)
    assert logged_out.status_code == 303
    assert logged_out.headers["location"] == "/admin/login"
    assert COOKIE not in client.cookies
    assert client.get("/admin").history[0].headers["location"] == "/admin/login"


def test_an_ended_session_says_so_on_the_login_page(fresh):
    """The cookie can outlive its signed expiry, which is reported as an ended session."""
    client, _, sessions = fresh
    claim(client)
    login(client)
    assert "You are signed in" in client.get("/admin").text

    sessions.now = lambda: datetime.now(tz=UTC) + timedelta(hours=13)
    ended = client.get("/admin", follow_redirects=False)
    assert ended.headers["location"] == "/admin/login?error=session_expired"
    assert "That session has ended." in client.get(ended.headers["location"]).text


def test_a_logout_without_a_live_session_clears_the_dead_cookie(fresh):
    """The session lapses in an open tab, and Log out is the next thing clicked."""
    client, _, sessions = fresh
    claim(client)
    login(client)
    sessions.now = lambda: datetime.now(tz=UTC) + timedelta(hours=13)

    stale = client.post("/v1/admin/logout", data={}, follow_redirects=False)
    assert stale.status_code == 303
    assert stale.headers["location"] == "/admin/login?error=session_expired"
    assert COOKIE not in client.cookies
    assert "That session has ended." in client.get(stale.headers["location"]).text
    assert client.get("/admin", follow_redirects=False).headers["location"] == "/admin/login"


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


def test_the_session_cookie_lasts_the_browser_session_not_its_signed_validity(fresh):
    """Set-Cookie is the contract: no Max-Age or Expires means until close."""
    client, _, _ = fresh
    claim(client)
    issued = login(client).headers["set-cookie"].lower()
    assert "max-age" not in issued
    assert "expires" not in issued


def test_a_settings_file_admin_cannot_read_names_the_file_and_the_key(fresh):
    """The docs send the operator to hand-edit settings.yaml, so it can be wrong."""
    client, wiring, _ = fresh
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
    assert COOKIE not in client.cookies

    settings_file.write_text(
        settings_file.read_text(encoding="utf-8").replace(
            "session_hours: 0", "session_hours: 8761"
        ),
        encoding="utf-8",
    )
    oversized = login(client)
    assert oversized.status_code == 500
    assert str(settings_file) in oversized.text
    assert "admin.session_hours" in oversized.text
    assert "less than or equal to 8760" in oversized.text
    assert COOKIE not in client.cookies

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
    """Recovery rotates the signing secret, so old cookies stop authenticating."""
    client, wiring, sessions = fresh
    claim(client)
    login(client)
    assert "You are signed in" in client.get("/admin").text

    (wiring.state_dir / "admin.json").unlink()
    (wiring.state_dir / "internal" / "claim-code").write_text(CLAIM_CODE + "\n", encoding="utf-8")
    reclaimed = claim(client, password="a different admin password")
    assert reclaimed.headers["location"] == "/admin/login"

    old_cookie = client.cookies.get(COOKIE)
    assert old_cookie is not None
    assert not sessions.valid(old_cookie)
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
    client, wiring, _ = fresh
    claim(client)
    record = wiring.state_dir / "admin.json"

    record.write_text('{"schema_version": 1, "password_h', encoding="utf-8")
    refused = login(client)
    assert refused.status_code == 500
    assert str(record) in refused.text
    assert "That password was not accepted." not in refused.text
    assert "claim Admin" in refused.text
    assert COOKIE not in client.cookies

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


def test_a_missing_or_damaged_session_secret_names_the_admin_record(fresh):
    client, wiring, _ = fresh
    claim(client)
    record = wiring.state_dir / "admin.json"
    intact = json.loads(record.read_text(encoding="utf-8"))

    for secret in (None, "not-hex", " " * 64):
        damaged = dict(intact)
        if secret is None:
            damaged.pop("session_secret")
        else:
            damaged["session_secret"] = secret
        record.write_text(json.dumps(damaged), encoding="utf-8")
        answered = login(client)
        assert answered.status_code == 500
        assert str(record) in answered.text
        assert "session_secret" in answered.text


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


def test_a_refused_claim_code_never_ends_a_live_session(fresh):
    """A rejected recovery does not rotate the secret backing the open cookie."""
    client, wiring, _ = fresh
    claim(client)
    login(client)
    record = wiring.state_dir / "admin.json"
    previous = record.read_bytes()
    record.unlink()
    (wiring.state_dir / "internal" / "claim-code").write_text(CLAIM_CODE, encoding="utf-8")

    refused = claim(client, code="not the code")
    assert refused.headers["location"] == "/admin/claim?error=invalid_claim_code"
    record.write_bytes(previous)
    assert "You are signed in" in client.get("/admin").text


def test_a_tampered_session_cookie_is_refused(fresh):
    client, _, _ = fresh
    claim(client)
    login(client)
    token = client.cookies.get(COOKIE)
    assert token is not None
    replacement = "A" if token[-1] != "A" else "B"
    client.cookies.set(COOKIE, token[:-1] + replacement)

    refused = client.get("/admin", follow_redirects=False)
    assert refused.headers["location"] == "/admin/login?error=session_expired"


def test_a_session_cookie_carrying_a_non_ascii_byte_returns_to_login(fresh):
    """Cookie headers are decoded as latin-1, so any high byte arrives as a character."""
    client, _, _ = fresh
    claim(client)
    login(client)

    refused = client.get(
        "/admin",
        headers={"cookie": f"{COOKIE}=v1.99999999999.abc.ézz".encode("latin-1")},
        follow_redirects=False,
    )
    assert refused.status_code == 303
    assert refused.headers["location"] == "/admin/login?error=session_expired"


def test_logout_clears_this_browser_but_not_a_token_taken_elsewhere(fresh):
    """The documented lifecycle: only re-claiming ends a session someone else holds."""
    client, wiring, sessions = fresh
    claim(client)
    login(client)
    captured = client.cookies.get(COOKIE)
    assert captured is not None

    client.post("/v1/admin/logout", data={}, follow_redirects=False)
    assert COOKIE not in client.cookies
    assert sessions.valid(captured)
    client.cookies.set(COOKIE, captured)
    assert "You are signed in" in client.get("/admin").text

    client.cookies.delete(COOKIE)
    (wiring.state_dir / "admin.json").unlink()
    (wiring.state_dir / "internal" / "claim-code").write_text(CLAIM_CODE, encoding="utf-8")
    reclaimed = claim(client, password="a different admin password")
    assert reclaimed.headers["location"] == "/admin/login"

    assert not sessions.valid(captured)
    client.cookies.set(COOKIE, captured)
    stale = client.get("/admin", follow_redirects=False)
    assert stale.headers["location"] == "/admin/login?error=session_expired"


def _assert_claim_state_refusal(response, path: Path) -> None:
    assert response.status_code == 500
    assert str(path) in response.text
    assert "Claim code" in response.text
    assert "your claim code is still good" in response.text
    assert "Restore that file from a backup" not in response.text


def test_a_claim_code_admin_cannot_read_is_not_reported_as_a_write(fresh):
    """Bootstrap writes that file and Admin only reads it, so the verb matters."""
    client, wiring, _ = fresh
    claim_code = wiring.state_dir / "internal" / "claim-code"
    claim_code.unlink()
    claim_code.mkdir()

    refused = claim(client)
    _assert_claim_state_refusal(refused, claim_code)
    assert f"could not read the claim code at {claim_code}" in refused.text
    assert "could not write" not in refused.text
    assert "free space" not in refused.text
    assert "readable by the user Admin runs as" in refused.text
    assert not (wiring.state_dir / "admin.json").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode this test relies on")
def test_a_state_directory_that_will_not_take_the_record_says_so_on_the_claim_page(fresh):
    """A full or read-only data volume is the first thing a fresh install can hit."""
    client, wiring, _ = fresh
    record = wiring.state_dir / "admin.json"
    wiring.state_dir.chmod(0o500)
    try:
        refused = claim(client)
    finally:
        wiring.state_dir.chmod(0o755)

    _assert_claim_state_refusal(refused, record)
    assert f"could not write {record}" in refused.text
    assert "writable by the user it runs as" in refused.text
    assert "free space" in refused.text
    assert "could not read the claim code" not in refused.text
    assert not record.exists()
    assert (wiring.state_dir / "internal" / "claim-code").is_file()
    assert claim(client).headers["location"] == "/admin/login"


def test_a_form_body_past_the_ceiling_is_refused_before_any_work(fresh):
    """Both public POSTs are unauthenticated, so neither buffers a body of any size."""
    client, wiring, _ = fresh
    oversized = {"code": CLAIM_CODE, "password": "x" * (MAX_FORM_BYTES + 1)}

    refused = client.post("/v1/admin/claim", data=oversized, follow_redirects=False)
    assert refused.status_code == 303
    assert refused.headers["location"] == "/admin/claim?error=too_large"
    assert "too large" in client.get(refused.headers["location"]).text
    assert not (wiring.state_dir / "admin.json").exists()
    assert (wiring.state_dir / "internal" / "claim-code").is_file()

    claim(client)
    refused = client.post(
        "/v1/admin/login",
        data={"password": "x" * (MAX_FORM_BYTES + 1)},
        follow_redirects=False,
    )
    assert refused.status_code == 303
    assert refused.headers["location"] == "/admin/login?error=too_large"
    assert "too large" in client.get(refused.headers["location"]).text
    assert COOKIE not in client.cookies

    assert login(client).headers["location"] == "/admin"
