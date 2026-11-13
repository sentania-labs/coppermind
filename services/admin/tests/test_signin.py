"""Getting in: the setup link, the password, signing in and out, and the form token."""

from __future__ import annotations

import json

from coppermind_admin.main import MAX_FORM_BYTES

from services.admin.tests.harness import PASSWORD, SETUP_CODE, post, set_up, token
from services.admin.tests.harness import admin as admin
from services.admin.tests.harness import fresh as fresh


def test_before_setup_every_page_leads_to_the_welcome_page(fresh):
    client, _, _ = fresh
    for path in ("/", "/admin", "/admin/notes", "/admin/signin"):
        response = client.get(path, follow_redirects=False)
        assert response.headers["location"] == "/admin/welcome", path
    page = client.get(f"/admin/welcome?code={SETUP_CODE}").text
    assert "Welcome to Coppermind" in page
    assert f'value="{SETUP_CODE}"' in page
    assert "Filled in from the link you opened." in page


def test_setup_takes_the_code_and_a_password_and_signs_its_owner_in(fresh):
    client, wiring, _ = fresh
    done = set_up(client)
    assert done.headers["location"] == "/admin/setup"
    record = wiring.state_dir / "admin.json"
    stored = record.read_text(encoding="utf-8")
    assert PASSWORD not in stored
    assert json.loads(stored)["password_hash"].startswith("$argon2")
    assert record.stat().st_mode & 0o077 == 0
    assert not (wiring.state_dir / "internal" / "claim-code").exists()
    page = client.get("/admin/setup").text
    assert "Password saved. You are signed in." in page
    assert "Setup guide" in page


def test_setup_says_what_is_wrong_in_words_and_keeps_the_code(fresh):
    client, wiring, _ = fresh
    wrong = set_up(client, code="WXYZ-WXYZ-WXYZ")
    assert wrong.status_code == 422
    assert "That setup code was not accepted." in wrong.text
    short = set_up(client, password="too short")
    assert "Use at least 12 characters. You typed 9." in short.text
    different = client.post(
        "/admin/welcome",
        data={
            "code": SETUP_CODE,
            "pw1": PASSWORD,
            "pw2": PASSWORD + "x",
            "csrf": token(client, "/admin/welcome"),
        },
    )
    assert "The two passwords are different." in different.text
    assert not (wiring.state_dir / "admin.json").exists()
    assert (wiring.state_dir / "internal" / "claim-code").exists()


def test_the_setup_code_is_typed_the_way_it_is_printed(fresh):
    client, _, _ = fresh
    assert set_up(client, code=f"  {SETUP_CODE.lower().replace('-', ' ')} ").status_code == 303


def test_setup_cannot_be_done_twice(admin):
    client, _, _ = admin
    client.cookies.clear()
    again = client.get("/admin/welcome", follow_redirects=False)
    assert again.headers["location"] == "/admin/signin"


def test_signing_out_and_in_again(admin):
    client, _, _ = admin
    out = post(client, "/admin/signout", page="/admin")
    assert out.headers["location"] == "/admin/signin?out=1"
    assert "You are signed out." in client.get(out.headers["location"]).text
    assert client.get("/admin/storage", follow_redirects=False).headers["location"] == (
        "/admin/signin"
    )
    refused = client.post(
        "/admin/signin", data={"password": "not it", "csrf": token(client, "/admin/signin")}
    )
    assert refused.status_code == 401
    assert "That password was not accepted. Try again." in refused.text
    back = client.post(
        "/admin/signin",
        data={"password": PASSWORD, "csrf": token(client, "/admin/signin")},
        follow_redirects=False,
    )
    assert back.headers["location"] == "/admin"
    assert client.get("/admin").status_code == 200


def test_someone_already_signed_in_is_not_asked_for_the_password_again(admin):
    client, _, _ = admin
    assert client.get("/", follow_redirects=False).headers["location"] == "/admin"
    assert client.get("/admin/signin", follow_redirects=False).headers["location"] == "/admin"


def test_a_button_without_the_page_token_changes_nothing(admin):
    client, _, store = admin
    refused = client.post("/admin/suggestions/pause", data={"paused": "1"})
    assert refused.status_code == 403
    assert "Nothing was changed." in refused.text
    assert store.asked("suggestions-pause") == []
    wrong = client.post("/admin/suggestions/pause", data={"csrf": "0" * 64})
    assert wrong.status_code == 403
    assert store.asked("suggestions-pause") == []


def test_too_much_sent_at_once_is_refused_in_words(admin):
    client, _, _ = admin
    big = client.post("/admin/suggestions/summary", data={"text": "x" * (MAX_FORM_BYTES + 1)})
    assert big.status_code == 413
    assert "too much to send in one go" in big.text


def session_cookie(client):
    (name,) = [cookie.name for cookie in client.cookies.jar if "session" in cookie.name.lower()]
    return name, client.cookies[name]


def test_a_new_setup_link_lets_a_forgotten_password_be_replaced(admin):
    client, wiring, _ = admin
    name, before = session_cookie(client)
    reset = wiring.state_dir / "internal" / "reset-code"
    reset.write_text("QRST-UVWX-YZ23\n", encoding="utf-8")
    client.cookies.clear()
    page = client.get("/admin/welcome?code=QRST-UVWX-YZ23").text
    assert "Choose a new password" in page
    new = "a different long password"
    done = client.post(
        "/admin/welcome",
        data={
            "code": "QRST-UVWX-YZ23",
            "pw1": new,
            "pw2": new,
            "csrf": token(client, "/admin/welcome"),
        },
        follow_redirects=False,
    )
    assert done.headers["location"] == "/admin"
    assert "New password saved." in client.get("/admin").text
    assert not reset.exists()
    client.cookies.clear()
    old = client.post(
        "/admin/signin", data={"password": PASSWORD, "csrf": token(client, "/admin/signin")}
    )
    assert old.status_code == 401
    client.cookies.set(name, before)
    replayed = client.get("/admin/storage", follow_redirects=False)
    assert replayed.headers["location"].startswith("/admin/signin")


def test_a_file_shaped_request_to_a_page_that_takes_no_file_is_refused(admin):
    client, _, store = admin
    # A body of this kind is not checked by the ordinary form check, so only the pages
    # that take a file, and check it themselves, may receive one.
    for path in ("/admin/suggestions/pause", "/admin/notes/delete-for-good", "/admin/signout"):
        refused = client.post(
            path, files={"x": ("x.txt", b"x", "text/plain")}, data={"paused": "1"}
        )
        assert refused.status_code == 403, path
        assert "nothing was changed" in refused.text
    assert store.calls == []
    assert client.get("/admin").status_code == 200


def test_the_new_setup_link_stops_working_after_an_hour(admin):
    import os
    import time

    client, wiring, _ = admin
    reset = wiring.state_dir / "internal" / "reset-code"
    reset.write_text("QRST-UVWX-YZ23\n", encoding="utf-8")
    old = time.time() - 3601
    os.utime(reset, (old, old))
    client.cookies.clear()
    late = client.get("/admin/welcome?code=QRST-UVWX-YZ23", follow_redirects=False)
    assert late.headers["location"] == "/admin/signin"


def test_the_password_is_changed_from_settings_and_the_old_one_stops_working(admin):
    client, _, _ = admin
    name, before = session_cookie(client)
    new = "a completely different phrase"
    wrong = client.post(
        "/admin/settings/signin",
        data={
            "cur": "not the password",
            "new1": new,
            "new2": new,
            "csrf": token(client, "/admin/settings/signin"),
        },
    )
    assert wrong.status_code == 422
    assert "That is not the current password." in wrong.text
    mismatch = client.post(
        "/admin/settings/signin",
        data={
            "cur": PASSWORD,
            "new1": new,
            "new2": new + "!",
            "csrf": token(client, "/admin/settings/signin"),
        },
    )
    assert "The two new passwords are different." in mismatch.text
    done = client.post(
        "/admin/settings/signin",
        data={
            "cur": PASSWORD,
            "new1": new,
            "new2": new,
            "csrf": token(client, "/admin/settings/signin"),
        },
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert client.get("/admin/storage", follow_redirects=False).status_code == 200
    _, after = session_cookie(client)
    assert after != before
    client.cookies.set(name, before)
    replayed = client.get("/admin/storage", follow_redirects=False)
    assert replayed.headers["location"].startswith("/admin/signin")
    client.cookies.set(name, after)
    assert client.get("/admin/storage", follow_redirects=False).status_code == 200
    client.cookies.clear()
    old = client.post(
        "/admin/signin", data={"password": PASSWORD, "csrf": token(client, "/admin/signin")}
    )
    assert old.status_code == 401
    fresh = client.post(
        "/admin/signin",
        data={"password": new, "csrf": token(client, "/admin/signin")},
        follow_redirects=False,
    )
    assert fresh.headers["location"] == "/admin"


def test_a_session_cookie_that_was_tampered_with_is_not_a_session(admin):
    client, _, _ = admin
    (name,) = [cookie.name for cookie in client.cookies.jar if "session" in cookie.name.lower()]
    genuine = client.cookies[name]
    client.cookies.set(name, genuine[:-4] + ("aaaa" if not genuine.endswith("aaaa") else "bbbb"))
    refused = client.get("/admin/storage", follow_redirects=False)
    assert refused.headers["location"].startswith("/admin/signin")
