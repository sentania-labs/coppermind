"""Move, rename, and folder endpoints through the API.

The API is stateless: every note operation is a call to the store. These tests
replace the store with a fake and verify the HTTP mapping.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from coppermind_api.deps import store
from coppermind_api.main import create_app
from fastapi.testclient import TestClient

from coppermind.api_keys import API_SCOPES, ApiKeySet, create_key
from coppermind.settings import Wiring
from coppermind.store_protocol import (
    FolderItem,
    FolderTree,
    MoveNote,
    NoteDocument,
    NotFound,
    PathCollision,
    RenameNote,
    StoreUnavailable,
    VersionConflict,
)

NOTE = NoteDocument(
    id="01K4Q8Z3N7V2X9M1B5C6D8E0F2",
    path="Review/2026-09-08 Ameren Architecture Sync.md",
    title="Ameren Architecture Sync",
    frontmatter={"id": "01K4Q8Z3N7V2X9M1B5C6D8E0F2", "type": "meeting"},
    body="# Ameren Architecture Sync\n",
    content_hash="sha256:abc",
    size_bytes=42,
    updated_at=datetime(2026, 9, 8, tzinfo=UTC),
)

MOVED = NOTE.model_copy(update={"path": "Work/2026-09-08 Ameren Architecture Sync.md"})

REMOVED = NOTE.model_copy(update={"path": "Review/Ameren Architecture Sync.md"})

KEY_ID = "a1b2c3d4e5f6a7b8"
KEY_SECRET = "unit-test-full-scope-secret"
KEY_RECORD, KEY = create_key(
    "unit test",
    list(API_SCOPES),
    key_id=KEY_ID,
    secret=KEY_SECRET,
    created_at=datetime(2026, 9, 8, tzinfo=UTC),
)

FOLDER_TREE = FolderTree(
    children=[
        FolderItem(name="Review", path="Review", note_count=1, children=[]),
        FolderItem(name="Work", path="Work", note_count=1, children=[]),
    ]
)


class FakeStore:
    """Satisfies the part of the store contract this slice uses."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.moved: tuple[str, MoveNote, str | None] | None = None
        self.renamed: tuple[str, RenameNote, str] | None = None
        self.folders_returned = 0
        self.key_reads = 0
        self.key_records = [KEY_RECORD]

    async def move_note(
        self, note_id: str, request: MoveNote, if_match: str | None
    ) -> NoteDocument:
        if self.error:
            raise self.error
        self.moved = (note_id, request, if_match)
        return MOVED

    async def rename_note(self, note_id: str, request: RenameNote, if_match: str) -> NoteDocument:
        if self.error:
            raise self.error
        self.renamed = (note_id, request, if_match)
        return REMOVED

    async def list_folders(self) -> FolderTree:
        if self.error:
            raise self.error
        self.folders_returned += 1
        return FOLDER_TREE

    async def is_ready(self) -> bool:
        return self.error is None

    async def get_api_keys(self) -> ApiKeySet:
        self.key_reads += 1
        return ApiKeySet(keys=list(self.key_records))


@pytest.fixture
def client(tmp_path: Path):
    token = tmp_path / "internal-token"
    token.write_text("test-token\n", encoding="utf-8")
    app = create_app(Wiring(internal_token_file=token, store_url="http://store.invalid:8081"))
    fake = FakeStore()

    app.dependency_overrides[store] = lambda: fake
    with TestClient(app) as test_client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        yield test_client, fake


# -- move tests ----------------------------------------------------------------


def test_a_move_returns_200_with_the_new_path(client):
    test_client, fake = client
    response = test_client.post(
        f"/v1/notes/{NOTE.id}/move",
        json={"target_folder": "Work"},
        headers={"Authorization": f"Bearer {KEY}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == NOTE.id
    assert body["path"] == "Work/2026-09-08 Ameren Architecture Sync.md"
    assert fake.moved == (NOTE.id, MoveNote(target_folder="Work"), None)


def test_a_move_with_if_match_forwards_it(client):
    test_client, fake = client
    response = test_client.post(
        f"/v1/notes/{NOTE.id}/move",
        json={"target_folder": "Work"},
        headers={"Authorization": f"Bearer {KEY}", "If-Match": '"sha256:abc"'},
    )
    assert response.status_code == 200
    assert fake.moved == (NOTE.id, MoveNote(target_folder="Work"), "sha256:abc")


def test_a_move_uses_notes_move_scope(client):
    """Move requires the notes:move scope."""
    test_client, fake = client
    # Use a key that only has notes:read
    read_key_record, read_key = create_key(
        "read only",
        ["notes:read"],
        key_id="b1c2d3e4f5a6b7c8",
        secret="unit-test-read-only-secret",
        created_at=datetime(2026, 9, 8, tzinfo=UTC),
    )
    fake.key_records = [read_key_record]
    response = test_client.post(
        f"/v1/notes/{NOTE.id}/move",
        json={"target_folder": "Work"},
        headers={"Authorization": f"Bearer {read_key}"},
    )
    assert response.status_code == 403


def test_a_move_preserves_typed_store_failures(client):
    test_client, fake = client
    fake.error = PathCollision("Work/existing.md")
    response = test_client.post(
        f"/v1/notes/{NOTE.id}/move",
        json={"target_folder": "Work"},
        headers={"Authorization": f"Bearer {KEY}"},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "path_collision"
    assert body["existing_path"] == "Work/existing.md"


def test_a_move_to_a_missing_note_returns_404(client):
    test_client, fake = client
    fake.error = NotFound("nonexistent")
    response = test_client.post(
        "/v1/notes/nonexistent/move",
        json={"target_folder": "Work"},
        headers={"Authorization": f"Bearer {KEY}"},
    )
    assert response.status_code == 404


# -- rename tests --------------------------------------------------------------


def test_a_rename_returns_200_with_the_updated_title(client):
    test_client, fake = client
    response = test_client.post(
        f"/v1/notes/{NOTE.id}/rename",
        json={"title": "Updated Title"},
        headers={"Authorization": f"Bearer {KEY}", "If-Match": '"sha256:abc"'},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == NOTE.id
    assert body["title"] == "Ameren Architecture Sync"  # from REMOVED doc
    assert fake.renamed == (NOTE.id, RenameNote(title="Updated Title"), "sha256:abc")


def test_a_rename_requires_if_match(client):
    """Rename without If-Match returns 428 precondition_required."""
    test_client, fake = client
    response = test_client.post(
        f"/v1/notes/{NOTE.id}/rename",
        json={"title": "Updated"},
        headers={"Authorization": f"Bearer {KEY}"},
    )
    assert response.status_code == 428
    assert response.json()["error"] == "precondition_required"
    assert fake.renamed is None


def test_a_rename_preserves_typed_store_failures(client):
    test_client, fake = client
    fake.error = VersionConflict("sha256:newer")
    response = test_client.post(
        f"/v1/notes/{NOTE.id}/rename",
        json={"title": "New"},
        headers={"Authorization": f"Bearer {KEY}", "If-Match": '"sha256:abc"'},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "version_conflict"


def test_a_rename_to_a_missing_note_returns_404(client):
    test_client, fake = client
    fake.error = NotFound("nonexistent")
    response = test_client.post(
        "/v1/notes/nonexistent/rename",
        json={"title": "New"},
        headers={"Authorization": f"Bearer {KEY}", "If-Match": '"sha256:abc"'},
    )
    assert response.status_code == 404


# -- folders tests -------------------------------------------------------------


def test_get_folders_returns_the_tree(client):
    test_client, fake = client
    response = test_client.get(
        "/v1/folders",
        headers={"Authorization": f"Bearer {KEY}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["children"] == FOLDER_TREE.model_dump()["children"]
    assert fake.folders_returned == 1


def test_get_folders_requires_notes_read(client):
    """GET /v1/folders requires notes:read scope."""
    test_client, fake = client
    write_key_record, write_key = create_key(
        "write only",
        ["notes:write"],
        key_id="c1d2e3f4a5b6c7d8",
        secret="unit-test-write-only-secret",
        created_at=datetime(2026, 9, 8, tzinfo=UTC),
    )
    fake.key_records = [write_key_record]
    response = test_client.get(
        "/v1/folders",
        headers={"Authorization": f"Bearer {write_key}"},
    )
    assert response.status_code == 403


def test_get_folders_preserves_store_errors(client):
    test_client, fake = client
    fake.error = StoreUnavailable("store down")
    response = test_client.get(
        "/v1/folders",
        headers={"Authorization": f"Bearer {KEY}"},
    )
    assert response.status_code == 503
    assert response.json()["error"] == "store_unavailable"
