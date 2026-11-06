"""`/v1/notices`: what was moved back, for the companion plugin or an agent, by scope."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from coppermind_api.deps import store
from coppermind_api.main import create_app
from fastapi.testclient import TestClient

from coppermind.api_keys import ApiKeySet, create_key
from coppermind.settings import Wiring
from coppermind.store_protocol import ReconcilerNotice

NOTE_ID = "01K4Q8Z3N7V2X9M1B5C6D8E0F2"
ARTIFACT_ID = "01K4Q8Z2A0P1Q2R3S4T5U6V7W8"
AT = datetime(2026, 11, 5, 20, 5, tzinfo=UTC)


class FakeStore:
    def __init__(self) -> None:
        created = datetime(2026, 9, 8, tzinfo=UTC)
        self.notes_record, self.notes_key = create_key(
            "notes", ["notes:read", "notes:write"], created_at=created
        )
        self.full_record, self.full_key = create_key(
            "full",
            ["notes:read", "notes:write", "artifacts:read", "artifacts:write"],
            created_at=created,
        )
        self.dismissed: list[str] = []
        self.notices = [
            ReconcilerNotice(
                note_id=ARTIFACT_ID,
                title="Enablement deck",
                from_path="Projects/deck.pptx",
                path="Artifacts/2026/2026-11/deck.pptx",
                reason="Moved back because artifacts are filed by the month they arrived.",
                at=AT,
                kind="artifact",
            ),
            ReconcilerNotice(
                note_id=NOTE_ID,
                title="A note",
                from_path="Elsewhere/A note.md",
                path="Notes/2026/2026-11/A note.md",
                reason="Notes file by date.",
                at=AT,
                kind="note",
            ),
        ]

    async def get_api_keys(self) -> ApiKeySet:
        return ApiKeySet(keys=[self.notes_record, self.full_record])

    async def list_reconciler_notices(self) -> list[ReconcilerNotice]:
        return list(self.notices)

    async def dismiss_reconciler_notice(self, note_id: str) -> None:
        self.dismissed.append(note_id)
        self.notices = [n for n in self.notices if n.note_id != note_id]

    async def is_ready(self) -> bool:
        return True


@contextmanager
def _client(tmp_path: Path):
    token = tmp_path / "internal-token"
    token.write_text("test-token\n", encoding="utf-8")
    app = create_app(Wiring(internal_token_file=token, store_url="http://store.invalid"))
    fake = FakeStore()
    app.dependency_overrides[store] = lambda: fake
    with TestClient(app) as client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        yield client, fake


@pytest.fixture
def api(tmp_path: Path):
    with _client(tmp_path) as pair:
        yield pair


def _as(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_notices_say_what_moved_from_where_to_where_and_why(api):
    client, fake = api
    assert client.get("/v1/notices").status_code == 401
    listed = client.get("/v1/notices", headers=_as(fake.full_key))
    assert listed.status_code == 200
    [artifact, note] = listed.json()
    assert artifact == {
        "note_id": ARTIFACT_ID,
        "title": "Enablement deck",
        "from_path": "Projects/deck.pptx",
        "path": "Artifacts/2026/2026-11/deck.pptx",
        "reason": "Moved back because artifacts are filed by the month they arrived.",
        "at": "2026-11-05T20:05:00Z",
        "kind": "artifact",
    }
    assert note["kind"] == "note" and note["note_id"] == NOTE_ID


def test_a_key_without_artifact_scopes_sees_and_dismisses_only_note_notices(api):
    client, fake = api
    listed = client.get("/v1/notices", headers=_as(fake.notes_key))
    assert [n["kind"] for n in listed.json()] == ["note"]
    refused = client.post(f"/v1/notices/{ARTIFACT_ID}/dismiss", headers=_as(fake.notes_key))
    assert refused.status_code == 403
    assert refused.json()["error"] == "forbidden"
    assert fake.dismissed == []
    allowed = client.post(f"/v1/notices/{NOTE_ID}/dismiss", headers=_as(fake.notes_key))
    assert allowed.status_code == 204
    assert fake.dismissed == [NOTE_ID]


def test_dismissing_hides_the_notice_and_a_second_dismiss_is_harmless(api):
    client, fake = api
    first = client.post(f"/v1/notices/{ARTIFACT_ID}/dismiss", headers=_as(fake.full_key))
    assert first.status_code == 204 and first.content == b""
    assert fake.dismissed == [ARTIFACT_ID]
    left = client.get("/v1/notices", headers=_as(fake.full_key)).json()
    assert [n["kind"] for n in left] == ["note"]
    again = client.post(f"/v1/notices/{ARTIFACT_ID}/dismiss", headers=_as(fake.full_key))
    assert again.status_code == 204
