"""Exercise real HTTP and Secure cookies on loopback, as compose smoke does."""

import os
import re
import shutil
import socket
import subprocess
import sys
import time

import pytest

from coppermind.api_keys import load_keys
from coppermind.settings import default_settings, read_settings
from coppermind.statefiles import StateStore
from services.admin.tests.test_keys_page import Inputs


def test_uvicorn_curl_uses_signed_in_csrf_for_keys_and_settings(tmp_path):
    if shutil.which("curl") is None:
        pytest.skip("curl is required for the real HTTP smoke regression")
    state = StateStore(tmp_path / "state")
    state.ensure("settings", default_settings().model_dump())
    claim_code = state.state_dir / "internal" / "claim-code"
    claim_code.parent.mkdir()
    claim_code.write_text("curl-test-claim")
    token = state.state_dir / "internal" / "internal-token"
    token.write_text("test-token")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    jar = str(tmp_path / "cookies")

    def curl(path, form=None):
        args = ["curl", "-sS", "--max-time", "5", "-b", jar, "-c", jar, "-w", "\n%{http_code}"]
        if form is not None:
            args += ["-X", "POST"]
            for name, value in form.items():
                args += ["--data-urlencode", f"{name}={value}"]
        result = subprocess.run(args + [base + path], capture_output=True, text=True, check=True)
        body, code = result.stdout.rsplit("\n", 1)
        return int(code), body

    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "coppermind_admin.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        env=os.environ | {"COPPERMIND_DATA_DIR": str(tmp_path)},
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    try:
        for _ in range(100):
            assert server.poll() is None, "Admin exited before it was ready"
            try:
                if curl("/healthz")[0] == 200:
                    break
            except subprocess.CalledProcessError:
                pass
            time.sleep(0.05)
        else:
            pytest.fail("Admin did not become ready")
        claim_token = Inputs(curl("/admin/claim")[1]).values["csrf"]
        assert (
            curl(
                "/v1/admin/claim",
                {
                    "csrf": claim_token,
                    "code": "curl-test-claim",
                    "password": "curl-test-password",
                },
            )[0]
            == 303
        )
        assert (
            curl(
                "/v1/admin/login",
                {
                    "csrf": claim_token,
                    "password": "curl-test-password",
                },
            )[0]
            == 303
        )
        # The old smoke reused Claim's unsigned token after login. It must fail.
        assert curl("/v1/admin/keys/create", {"csrf": claim_token})[0] == 403
        overview_token = Inputs(curl("/admin")[1]).values["csrf"]
        assert overview_token != claim_token
        form = Inputs(curl("/admin/keys")[1]).values
        assert form["csrf"] == overview_token
        code, created = curl(
            "/v1/admin/keys/create",
            form
            | {
                "name": "curl-read-only",
                "scope:notes:read": "on",
            },
        )
        assert code == 201
        match = re.search(r'<pre id="credential">([^<]+)</pre>', created)
        assert match is not None
        credential = match[1]
        code, listed = curl("/admin/keys")
        assert code == 200 and credential not in listed and "curl-read-only" in listed
        key = load_keys(state).keys[0]
        code, confirmation = curl(f"/v1/admin/keys/{key.key_id}/confirm", Inputs(listed).values)
        assert code == 200
        assert curl(f"/v1/admin/keys/{key.key_id}/revoke", Inputs(confirmation).values)[0] == 200
        assert load_keys(state).keys[0].revoked_at is not None
        form = Inputs(curl("/admin/settings")[1]).values | {"git.debounce_s": "17"}
        revision = state.read("settings").revision
        code, saved = curl("/v1/admin/settings", form)
        assert code == 200 and f"Saved revision {revision + 1}" in saved
        assert Inputs(curl("/admin/settings")[1]).values["git.debounce_s"] == "17"
        assert read_settings(state).git.debounce_s == 17
        assert state.read("settings").revision == revision + 1
        assert curl("/v1/admin/settings", form)[0] == 409
        assert curl("/v1/admin/logout", {"csrf": overview_token})[0] == 303
        assert curl("/admin/keys")[0] == 303
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
