"""Signed-in browser shared by Admin page tests."""

import pytest

from services.admin.tests.test_admin import _client, claim, login


@pytest.fixture(name="signed_in")
def admin_browser(tmp_path):
    client, wiring, _ = _client(tmp_path)
    with client:
        claim(client)
        login(client)
        yield client, wiring
