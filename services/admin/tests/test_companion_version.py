import json
import re
import shutil

import pytest
from coppermind_admin.pages import home

from services.admin.tests.harness import admin as admin


def test_version_changes_with_each_shipped_file(tmp_path, monkeypatch):
    shutil.copytree(home.PLUGIN, tmp_path / "plugin")
    monkeypatch.setattr(home, "PLUGIN", tmp_path / "plugin")
    original = json.loads(home.companion_files()["manifest.json"])["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", original)
    for name in home.PLUGIN_FILES:
        path = home.PLUGIN / name
        content = path.read_bytes()
        if name == "manifest.json":
            manifest = json.loads(content)
            manifest["description"] += " changed"
            path.write_text(json.dumps(manifest))
        else:
            path.write_bytes(content + b" changed")
        assert json.loads(home.companion_files()["manifest.json"])["version"] != original
        path.write_bytes(content)


@pytest.mark.parametrize("installed", [None, "0.1.0"])
def test_outdated_companion_offers_update(admin, installed):
    client, _, store = admin
    store.views["overview"]["companion"].update(placed=True, version=installed)
    page = client.get("/admin/setup/companion").text
    assert "Update the companion" in page
    assert (
        "Could not read the companion version" in page
        if installed is None
        else "out of date" in page
    )
    assert page.count('action="/admin/setup/companion/place"') == 1


def test_current_companion_shows_version(admin):
    client, _, store = admin
    version = json.loads(home.companion_files()["manifest.json"])["version"]
    store.views["overview"]["companion"].update(placed=True, version=version)
    page = client.get("/admin/setup/companion").text
    assert f"Companion version {version} is current" in page
    assert "Update the companion" not in page


def test_store_reads_installed_manifest(tmp_path):
    from types import SimpleNamespace

    from coppermind_store.admin_service import PLUGIN_FOLDER, AdminService

    service = object.__new__(AdminService)
    service.store = SimpleNamespace(notes_root=tmp_path)
    folder = tmp_path / PLUGIN_FOLDER
    folder.mkdir(parents=True)
    assert service.companion_version() is None
    (folder / "manifest.json").write_text("{broken")
    assert service.companion_version() is None
    (folder / "manifest.json").write_text('{"version": "1.2.3"}')
    assert service.companion_version() == "1.2.3"


def test_update_installs_current_files_and_explains_reload(admin):
    import base64

    from services.admin.tests.harness import follow, post

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
    } == home.companion_files()
    message = follow(client, response)
    assert "Reload the companion in Obsidian" in message
    assert "Paired devices stay paired" in message
