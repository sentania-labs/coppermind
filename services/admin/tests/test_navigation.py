"""Authenticated navigation stays useful across GETs and form submissions."""

from services.admin.tests.browser import admin_browser as admin_browser
from services.admin.tests.test_admin import _client
from services.admin.tests.test_settings_page import section_form


def test_navigation_and_saved_section(signed_in):
    client, _ = signed_in
    for name in ("keys", "properties", "settings"):
        response = client.get(f"/admin/{name}")
        assert response.status_code == 200
        assert f'<a href="/admin/{name}" aria-current="page">' in response.text
        assert response.text.count('action="/v1/admin/logout"') == 1
    response = client.post("/v1/admin/settings?section=git", data=section_form(client, "git"))
    assert response.status_code == 200
    assert '<a href="/admin/settings" aria-current="page">' in response.text


def test_public_claim_has_no_operator_navigation(tmp_path):
    client, _, _ = _client(tmp_path)
    with client:
        response = client.get("/admin/claim")
    assert '<nav aria-label="Admin">' not in response.text
    assert 'action="/v1/admin/logout"' not in response.text
