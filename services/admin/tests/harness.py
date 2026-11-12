"""A signed-in Admin with a stand-in for the store, shared by the Admin tests.

The views the stand-in answers with were captured from a running Coppermind that
held only made-up notes, people and recordings, so pages are drawn from data of
the real shape. A test changes one answer to put a page into the state it is about.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from coppermind_admin.auth import AdminCredentials, SignedSessions
from coppermind_admin.main import create_app
from fastapi.testclient import TestClient

from coppermind.schema import default_schema
from coppermind.settings import Wiring, default_settings
from coppermind.statefiles import StateStore
from coppermind.store_protocol import StoreError

PASSWORD = "correct horse battery staple"
SETUP_CODE = "ABCD-EFGH-JKLM"
VIEWS: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "views.json").read_text(encoding="utf-8")
)


class FakeStore:
    """Answers the way the store does, and remembers what it was asked."""

    def __init__(self) -> None:
        self.views = copy.deepcopy(VIEWS)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.context_calls: list[tuple[str, dict[str, Any]]] = []
        self.patches: list[tuple[str, Any]] = []
        self.calendar_files: list[tuple[str, bytes]] = []
        self.answers: dict[str, Any] = {}
        self.down = False

    def asked(self, action: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.calls if name == action]

    async def admin_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.down:
            raise StoreError("the store did not answer")
        self.calls.append((action, payload))
        answer = self.answers.get(action, self.views.get(action, {}))
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = answer(payload)
        return copy.deepcopy(answer)

    async def context_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.down:
            raise StoreError("the store did not answer")
        self.context_calls.append((action, payload))
        answer = self.answers.get("context:" + action, self.views.get(action, {}))
        if isinstance(answer, Exception):
            raise answer
        return copy.deepcopy(answer)

    async def add_calendar_file(self, filename: str, data: Any) -> dict[str, Any]:
        if self.down:
            raise StoreError("the store did not answer")
        body = b"".join([chunk async for chunk in data])
        self.calendar_files.append((filename, body))
        return copy.deepcopy(self.answers.get("calendar-add", {}))

    async def patch_frontmatter(self, identity: str, patch: Any, *, if_match: str) -> None:
        self.patches.append((identity, patch))

    async def aclose(self) -> None:
        return None


def build(tmp_path: Path) -> tuple[TestClient, Wiring, FakeStore]:
    """An Admin that nobody has set up yet, with its setup code in place."""
    wiring = Wiring(data_dir=tmp_path / "data")
    state = StateStore(wiring.state_dir)
    state.ensure("settings", default_settings().model_dump(mode="json"))
    state.ensure("schema", default_schema().model_dump(mode="json"))
    code = wiring.state_dir / "internal" / "claim-code"
    code.parent.mkdir(parents=True)
    code.write_text(SETUP_CODE + "\n", encoding="utf-8")
    sessions = SignedSessions(AdminCredentials(wiring.state_dir))
    app = create_app(wiring, sessions)
    store = FakeStore()
    app.state.store = store
    return TestClient(app, base_url="https://testserver"), wiring, store


def token(client: TestClient, path: str) -> str:
    """The form token a page carries, the way a browser would send it back."""
    match = re.search(r'name="csrf" value="([a-f0-9]+)"', client.get(path).text)
    assert match is not None, f"{path} carries no form token"
    return match[1]


def set_up(client: TestClient, code: str = SETUP_CODE, password: str = PASSWORD) -> Any:
    return client.post(
        "/admin/welcome",
        data={
            "code": code,
            "pw1": password,
            "pw2": password,
            "csrf": token(client, "/admin/welcome"),
        },
        follow_redirects=False,
    )


def post(client: TestClient, path: str, data: dict[str, Any] | None = None, page: str = "") -> Any:
    """Press a button on `page` (the form's own page when not given)."""
    fields = dict(data or {})
    fields["csrf"] = token(client, page or "/admin/suggestions/fill")
    return client.post(path, data=fields, follow_redirects=False)


def follow(client: TestClient, response: Any) -> str:
    """The page a button's result lands on, with its one result message."""
    assert response.status_code == 303, response.text[:400]
    return client.get(response.headers["location"]).text


Change = Callable[[dict[str, Any]], None]


@pytest.fixture
def fresh(tmp_path):
    """Admin before anyone has set it up."""
    client, wiring, store = build(tmp_path)
    with client:
        yield client, wiring, store


@pytest.fixture
def admin(tmp_path):
    """Admin with its owner signed in."""
    client, wiring, store = build(tmp_path)
    with client:
        assert set_up(client).status_code == 303
        yield client, wiring, store
