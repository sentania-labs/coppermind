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
        ("/admin/context", "Calendar"),
        ("/admin/context?tab=calendar", "Calendar"),
        ("/admin/context?tab=review", "Settings"),
        ("/admin/context?tab=review&offset=50", "Settings"),
        ("/admin/context?tab=models", "Settings"),
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


def test_no_page_links_back_to_an_overview(signed_in):
    """The landing page is Home; no page may still call it Overview."""
    import re

    client, _ = signed_in
    for path in (
        "/admin",
        "/admin/artifacts",
        "/admin/search-index",
        "/admin/fields",
        "/admin/sync",
    ):
        text = client.get(path).text
        assert not re.search(r"<a [^>]*>\s*(Admin )?overview\s*</a>", text, re.I), path


def test_public_claim_has_no_operator_navigation(tmp_path):
    client, _, _ = _client(tmp_path)
    with client:
        response = client.get("/admin/claim")
    assert '<nav aria-label="Admin">' not in response.text
    assert 'action="/v1/admin/logout"' not in response.text


def test_shell_palette_meets_normal_text_contrast():
    """The delivered theme colors meet WCAG's 4.5:1 normal-text threshold."""
    import re

    from coppermind_admin.layout import render

    document = render("Home", "<h1>Home</h1>", "/admin")
    light = re.search(r':root, \[data-theme="light"\] \{(.*?)\}', document, re.S)
    dark = re.search(r'\[data-theme="dark"\] \{(.*?)\}', document, re.S)
    assert light and dark

    def colors(block):
        values = dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block))

        def resolve(name):
            value = values[name].strip()
            return resolve(value[4:-1]) if value.startswith("var(") else value

        return resolve

    def luminance(color):
        channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    for block in (light.group(1), dark.group(1)):
        color = colors(block)
        for foreground, background in (
            ("--on-accent", "--accent"),
            ("--on-accent", "--bad-ink"),
            ("--link", "--surface"),
            ("--link", "--surface-sunken"),
            ("--on-accent", "--accent-strong"),
        ):
            a, b = luminance(color(foreground)), luminance(color(background))
            assert (max(a, b) + 0.05) / (min(a, b) + 0.05) >= 4.5
