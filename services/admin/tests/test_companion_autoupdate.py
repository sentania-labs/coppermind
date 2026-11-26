import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from coppermind_admin.main import update_companion
from coppermind_admin.pages import home
from coppermind_store.admin_service import PLUGIN_FOLDER, AdminService

from coppermind.settings import ProductSettings
from services.admin.tests.harness import admin as admin


class Context:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.saved = {}

    def read(self, name, default):
        return self.saved.get(name, default)

    def write(self, name, value):
        self.saved[name] = value


def service(tmp_path, *, auto=True):
    sync = SimpleNamespace(companion_auto_update=auto)
    stub = AdminService.__new__(AdminService)
    stub.store = SimpleNamespace(
        notes_root=tmp_path,
        control=SimpleNamespace(settings=lambda: SimpleNamespace(sync=sync)),
    )
    stub.context = Context()
    return stub


def payload():
    files = {n: base64.b64encode(d).decode() for n, d in home.companion_files().items()}
    return {"files": files, "url": "https://cm.example"}


def place(tmp_path, version="0.0.1", main=b"old"):
    folder = tmp_path / PLUGIN_FOLDER
    folder.mkdir(parents=True)
    (folder / "main.js").write_bytes(main)
    (folder / "styles.css").write_bytes(b"")
    (folder / "manifest.json").write_text(json.dumps({"id": "x", "version": version}))
    return folder


def run(stub):
    return asyncio.run(stub.companion_autoupdate(payload()))


def test_updates_an_older_companion_keeps_data_and_records_a_notice(tmp_path):
    folder = place(tmp_path)
    (folder / "data.json").write_text('{"url": "https://mine", "key": "k"}')
    stub = service(tmp_path)
    answer = run(stub)
    new = json.loads(home.companion_files()["manifest.json"])["version"]
    assert answer == {"state": "updated", "from": "0.0.1", "to": new}
    assert (folder / "main.js").read_bytes() == home.companion_files()["main.js"]
    assert json.loads((folder / "data.json").read_text()) == {"url": "https://mine", "key": "k"}
    notice = stub.context.saved["companion-updated"]
    assert (notice["from"], notice["to"], notice["dismissed"]) == ("0.0.1", new, False)
    shown = {n["id"] for n in AdminService._notices(stub, [])}
    assert shown == {"companion-updated"}


def test_does_nothing_when_current_and_does_not_rewrite(tmp_path):
    stub = service(tmp_path)
    folder = tmp_path / PLUGIN_FOLDER
    folder.mkdir(parents=True)
    for name, data in home.companion_files().items():
        (folder / name).write_bytes(data)
    before = {p.name: p.stat().st_mtime_ns for p in folder.iterdir()}
    assert run(stub) == {"state": "current"}
    assert {p.name: p.stat().st_mtime_ns for p in folder.iterdir()} == before
    assert "companion-updated" not in stub.context.saved


def test_does_nothing_when_no_companion_is_in_the_vault(tmp_path):
    stub = service(tmp_path)
    assert run(stub) == {"state": "absent"}
    assert not (tmp_path / PLUGIN_FOLDER).exists()


def test_respects_the_setting(tmp_path):
    folder = place(tmp_path)
    assert run(service(tmp_path, auto=False)) == {"state": "off"}
    assert (folder / "main.js").read_bytes() == b"old"


def test_a_failed_install_is_recorded_not_raised(tmp_path, monkeypatch):
    place(tmp_path)
    stub = service(tmp_path)

    async def broken(self, body):
        raise OSError("disk full")

    monkeypatch.setattr(AdminService, "companion_install", broken)
    assert run(stub) == {"state": "failed"}
    assert stub.context.saved["companion-update-failed"]["error"] == "disk full"
    assert "companion-updated" not in stub.context.saved


def test_dismissing_the_notice_clears_it(tmp_path):
    stub = service(tmp_path)
    stub.context.saved["companion-updated"] = {"from": "1.0.0", "to": "1.0.1", "dismissed": False}
    asyncio.run(stub.notice_dismiss({"id": "companion-updated"}))
    assert AdminService._notices(stub, []) == []


def test_setting_defaults_on():
    assert ProductSettings().sync.companion_auto_update is True


def test_admin_start_up_asks_the_store_once_in_the_background(admin, monkeypatch):
    client, wiring, store = admin
    waits = []

    async def quick(seconds):
        waits.append(seconds)

    monkeypatch.setattr("coppermind_admin.main.asyncio.sleep", quick)
    store.answers["companion-autoupdate"] = {"state": "current"}
    asyncio.run(update_companion(client.app, wiring))
    assert waits == [2]
    assert len(store.asked("companion-autoupdate")) == 1
    assert set(store.asked("companion-autoupdate")[0]) == {"files", "url"}


def test_admin_start_up_retries_until_the_store_answers(admin, monkeypatch):
    client, wiring, store = admin
    waits = []

    async def quick(seconds):
        waits.append(seconds)
        store.down = len(waits) < 3

    monkeypatch.setattr("coppermind_admin.main.asyncio.sleep", quick)
    store.down = True
    store.answers["companion-autoupdate"] = {"state": "current"}
    asyncio.run(update_companion(client.app, wiring))
    assert waits == [2, 5, 10]
    assert len(store.asked("companion-autoupdate")) == 1


def test_admin_start_up_gives_up_quietly_when_the_store_never_comes_up(admin, monkeypatch):
    client, wiring, store = admin
    waits = []

    async def quick(seconds):
        waits.append(seconds)

    monkeypatch.setattr("coppermind_admin.main.asyncio.sleep", quick)
    store.down = True
    asyncio.run(update_companion(client.app, wiring))
    assert waits == [2, 5, 10, 30, 60, 120, 300]
    assert store.calls == []


def test_notice_shows_on_home_and_setup_and_the_setting_is_on_sync(admin):
    client, _, store = admin
    store.views["overview"]["notices"] = [
        {
            "id": "companion-updated",
            "kind": "companion_updated",
            "note_id": "",
            "reference": "",
            "reason": "",
            "from": "0.9.1",
            "to": "0.9.3",
        }
    ]
    for path in ("/admin", "/admin/setup/companion"):
        page = client.get(path).text
        assert "The companion was updated from 0.9.1 to 0.9.3" in page
        assert "Reload Obsidian on each device" in page
    assert "/admin/sync/companion-auto" in client.get("/admin/sync").text


@pytest.mark.parametrize("on", ["", "1"])
def test_sync_page_toggle_saves_the_setting(admin, on):
    client, wiring, _ = admin
    from services.admin.tests.harness import post

    post(client, "/admin/sync/companion-auto", {"on": on}, "/admin/sync")
    from coppermind.settings import read_settings
    from coppermind.statefiles import StateStore

    assert read_settings(StateStore(wiring.state_dir)).sync.companion_auto_update is bool(on)
