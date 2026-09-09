"""The public contract, with a stand-in store.

The API is stateless and every note operation is a call to the store, so these
tests replace the store with an object that satisfies the same contract. What
is under test is the mapping: status codes, the ETag, the error envelope, and
the fact that nothing here touches a file.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from coppermind_api.deps import store
from coppermind_api.main import create_app
from fastapi.testclient import TestClient

from coppermind.settings import Wiring
from coppermind.store_protocol import (
    CreateNote,
    MetadataUnavailable,
    NoteDocument,
    NotFound,
    RawNote,
    ValidationFailed,
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


class FakeStore:
    """Satisfies the part of the store contract this slice uses."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.created: CreateNote | None = None

    async def create_note(self, request: CreateNote) -> NoteDocument:
        if self.error:
            raise self.error
        self.created = request
        return NOTE

    async def get_note(self, note_id: str) -> NoteDocument:
        if self.error:
            raise self.error
        return NOTE

    async def read_raw(self, note_id: str) -> RawNote:
        if self.error:
            raise self.error
        return RawNote(
            id=NOTE.id,
            path=NOTE.path,
            text="---\nid: x\n---\n# Title\n",
            content_hash=NOTE.content_hash,
        )

    async def is_ready(self) -> bool:
        return self.error is None


@pytest.fixture
def client(tmp_path: Path):
    token = tmp_path / "internal-token"
    token.write_text("test-token\n", encoding="utf-8")
    app = create_app(Wiring(internal_token_file=token, store_url="http://store.invalid:8081"))
    fake = FakeStore()

    app.dependency_overrides[store] = lambda: fake
    with TestClient(app) as test_client:
        # Readiness reads the client off application state rather than through
        # the dependency, so it is replaced there too.
        app.state.store = fake
        yield test_client, fake


def test_health_says_the_process_is_up(client):
    test_client, _ = client
    response = test_client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["service"] == "coppermind-api"


def test_readiness_follows_the_store(client):
    test_client, fake = client
    assert test_client.get("/readyz").status_code == 200

    fake.error = MetadataUnavailable("postgres is down")
    response = test_client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["ready"] is False


def test_creating_a_note_answers_201_with_an_etag_and_a_location(client):
    test_client, fake = client
    response = test_client.post("/v1/notes", json={"title": "Ameren Architecture Sync"})
    assert response.status_code == 201
    assert response.headers["etag"] == '"sha256:abc"'
    assert response.headers["location"] == f"/v1/notes/{NOTE.id}"
    assert response.json()["path"] == NOTE.path
    assert fake.created is not None and fake.created.created_by == "api"


def test_a_note_needs_a_title(client):
    test_client, _ = client
    assert test_client.post("/v1/notes", json={"title": ""}).status_code == 422


def test_reading_a_note_returns_the_document_and_its_etag(client):
    test_client, _ = client
    response = test_client.get(f"/v1/notes/{NOTE.id}")
    assert response.status_code == 200
    assert response.headers["etag"] == '"sha256:abc"'
    assert response.json()["id"] == NOTE.id


def test_asking_for_markdown_returns_the_file_itself(client):
    test_client, _ = client
    response = test_client.get(f"/v1/notes/{NOTE.id}", headers={"Accept": "text/markdown"})
    assert response.status_code == 200
    assert response.text.startswith("---")
    assert response.headers["content-type"].startswith("text/markdown")


def test_a_missing_note_is_a_404_in_the_error_envelope(client):
    test_client, fake = client
    fake.error = NotFound("nope")
    response = test_client.get("/v1/notes/nope")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_a_database_outage_is_a_clean_503_that_says_the_files_are_fine(client):
    test_client, fake = client
    fake.error = MetadataUnavailable("connection refused")
    response = test_client.post("/v1/notes", json={"title": "During an outage"})
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "metadata_unavailable"
    assert "notes filesystem is unaffected" in body["message"]


def test_a_schema_violation_comes_back_as_422_with_the_reasons(client):
    test_client, fake = client
    fake.error = ValidationFailed(["account: required when context is one of customer"])
    response = test_client.post("/v1/notes", json={"title": "Missing account"})
    assert response.status_code == 422
    assert response.json()["errors"] == ["account: required when context is one of customer"]
