import json
from types import SimpleNamespace

import pytest
from coppermind_admin.pages import companion
from coppermind_store.admin_service import PLUGIN_FOLDER, AdminService

from coppermind.companion import fingerprint
from services.admin.tests.harness import admin as admin
from services.admin.tests.harness import follow, post


def test_release_version_and_file_changes(monkeypatch):
    monkeypatch.setenv("COPPERMIND_BUILD_VERSION", "v2.3.4")
    files = companion.companion_files()
    assert json.loads(files["manifest.json"])["version"] == "2.3.4"
    original = fingerprint(files)
    monkeypatch.setenv("COPPERMIND_BUILD_VERSION", "v2.3.5")
    assert fingerprint(companion.companion_files()) == original
    for name in ("main.js", "styles.css"):
        changed = dict(files)
        changed[name] += b"changed"
        assert fingerprint(changed) != original
    manifest = json.loads(files["manifest.json"])
    manifest["description"] += " changed"
    changed = dict(files, **{"manifest.json": json.dumps(manifest).encode()})
    assert fingerprint(changed) != original


@pytest.mark.parametrize("installed", [None, "0.1.0"])
def test_outdated_companion_offers_update_on_setup_and_in_the_obsidian_line(admin, installed):
    client, _, store = admin
    store.views["overview"]["companion"].update(placed=True, version=installed, fingerprint="old")
    page = client.get("/admin/setup/companion").text
    assert "Update the companion" in page
    assert (
        "Could not read the companion version" in page
        if installed is None
        else "You have version 0.1.0" in page
    )
    assert "The update installs version" in page
    assert page.count('action="/admin/setup/companion/place"') == 1
    line = client.get("/admin").text
    assert "An update to version" in line
    assert 'action="/admin/setup/companion/place"' in line
    page = client.get("/admin/sync").text
    assert "Update available" in page
    assert "/admin/setup/companion" in page
    assert "Put the companion in my vault again" in page


def test_current_companion_and_release_only_change_do_not_nag(admin, monkeypatch):
    client, _, store = admin
    monkeypatch.setenv("COPPERMIND_BUILD_VERSION", "v2.3.5")
    store.views["overview"]["companion"].update(
        placed=True, version="2.3.4", fingerprint=fingerprint(companion.companion_files())
    )
    page = client.get("/admin/setup/companion").text
    assert "Companion version 2.3.4 is current" in page
    for path in ("/admin", "/admin/sync", "/admin/setup/companion"):
        page = client.get(path).text
        assert "Update available" not in page
        assert "An update to version" not in page


def test_store_reads_version_and_detects_modified_files(tmp_path):
    service = object.__new__(AdminService)
    service.store = SimpleNamespace(notes_root=tmp_path)
    folder = tmp_path / PLUGIN_FOLDER
    folder.mkdir(parents=True)
    assert service.companion_version() is None
    assert service.companion_fingerprint() is None
    for name, data in companion.companion_files().items():
        (folder / name).write_bytes(data)
    assert service.companion_fingerprint() == fingerprint(companion.companion_files())
    (folder / "main.js").write_bytes(b"modified")
    assert service.companion_fingerprint() != fingerprint(companion.companion_files())
    (folder / "manifest.json").write_text('{"version": "1.0.209927058280035"}')
    assert service.companion_version() is None
    (folder / "manifest.json").write_text("{broken")
    assert service.companion_version() is None
    assert service.companion_fingerprint() is None


def test_update_installs_current_files_and_explains_reload(admin):
    import base64

    client, _, store = admin
    store.views["overview"]["companion"].update(placed=True, version="0.1.0")
    response = post(
        client,
        "/admin/setup/companion/place",
        {"again": "1", "back": "/admin/setup/companion"},
        page="/admin/setup/companion",
    )
    installed = store.asked("companion-install")[-1]["files"]
    assert {
        name: base64.b64decode(data) for name, data in installed.items()
    } == companion.companion_files()
    message = follow(client, response)
    assert "Reload the companion in Obsidian" in message
    assert "Paired devices stay paired" in message
