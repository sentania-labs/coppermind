"""Authenticated navigation stays useful across GETs and form submissions."""

import pytest

from services.admin.tests.browser import admin_browser as admin_browser
from services.admin.tests.test_admin import _client
from services.admin.tests.test_settings_page import section_form


def test_navigation_and_saved_section(signed_in):
    client, _ = signed_in
    for name in ("keys", "properties", "settings"):
        response = client.get(f"/admin/{name}")
        assert response.status_code == 200
        # One form in the compact mobile menu, one in the desktop controls.
        assert response.text.count('action="/v1/admin/logout"') == 2
    assert '<a href="/admin/settings" aria-current="page">' in response.text
    assert '<a href="/admin/keys">Connected apps</a>' in response.text
    response = client.post("/v1/admin/settings?section=git", data=section_form(client, "git"))
    assert response.status_code == 200
    assert '<a href="/admin/settings" aria-current="page">' in response.text


@pytest.mark.parametrize(
    ("path", "current"),
    [
        *[
            (f"/admin/{name}", "Settings")
            for name in (
                "settings",
                "keys",
                "sync",
                "search-index",
                "artifacts",
                "purge",
                "note-history",
                "note-history/abc",
                "field-index",
                "properties",
            )
        ],
        ("/admin/workflow?tab=enrichment", "Settings"),
        ("/admin/context/jobs/job1", "Settings"),
        ("/admin/notes/abc", "Review status"),
        ("/admin/review/abc", "Review status"),
        ("/admin/workflow?tab=review", "Review status"),
        ("/admin/workflow?tab=intake", "Imports"),
        ("/admin", "Home"),
    ],
)
def test_each_page_highlights_its_menu_section(path, current):
    import re

    from coppermind_admin.layout import render

    html = render("Page", "<h1>Page</h1>", path)
    desktop = re.search(r'<nav class="desktop-nav"[^>]*>(.*?)</nav>', html, re.S)
    assert desktop
    assert re.findall(r'aria-current="page">([^<]*)<', desktop.group(1)) == [current]
    assert ("Devices, connections and maintenance" in html) == (current == "Settings")


def test_public_claim_has_no_operator_navigation(tmp_path):
    client, _, _ = _client(tmp_path)
    with client:
        response = client.get("/admin/claim")
    assert '<nav aria-label="Admin">' not in response.text
    assert 'action="/v1/admin/logout"' not in response.text
