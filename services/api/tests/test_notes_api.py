"""The public contract, with a stand-in store.

The API is stateless and every note operation is a call to the store, so these
tests replace the store with an object that satisfies the same contract. What
is under test is the mapping: status codes, the ETag, the error envelope, and
the fact that nothing here touches a file.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from coppermind_api import __version__
from coppermind_api.deps import store
from coppermind_api.main import create_app
from fastapi.testclient import TestClient

from coppermind.settings import Wiring
from coppermind.store_protocol import (
    CreateNote,
    MetadataUnavailable,
    NoteDocument,
    NotesFilesystemUnavailable,
    NoteUnparseable,
    NotFound,
    StoreUnavailable,
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


def api_app(tmp_path: Path, build_version: str | None = None):
    token = tmp_path / "internal-token"
    token.write_text("test-token\n", encoding="utf-8")
    return create_app(
        Wiring(
            internal_token_file=token,
            store_url="http://store.invalid:8081",
            build_version=build_version,
        )
    )


def test_healthz_reports_the_version_the_image_was_built_from(tmp_path: Path):
    """CI stamps the tag it built from; a working tree run reports the package version."""
    with TestClient(api_app(tmp_path)) as plain:
        assert plain.get("/healthz").json()["version"] == __version__

    with TestClient(api_app(tmp_path, build_version="v1.2.0")) as stamped:
        assert stamped.get("/healthz").json()["version"] == "v1.2.0"


def test_readiness_follows_the_store(client):
    test_client, fake = client
    assert test_client.get("/readyz").status_code == 200

    fake.error = MetadataUnavailable("postgres is down")
    response = test_client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["ready"] is False


def test_creating_a_note_answers_201_with_an_etag_and_a_location(client):
    test_client, fake = client
    response = test_client.post("/v1/notes", json={"title": "  Ameren Architecture Sync  "})
    assert response.status_code == 201
    assert response.headers["etag"] == '"sha256:abc"'
    assert response.headers["location"] == f"/v1/notes/{NOTE.id}"
    assert response.json()["path"] == NOTE.path
    assert fake.created is not None and fake.created.title == "Ameren Architecture Sync"


def test_a_note_needs_a_title(client):
    test_client, _ = client
    response = test_client.post("/v1/notes", json={"title": ""})
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert body["errors"] == ["title: String should have at least 1 character"]


def test_a_note_title_cannot_be_only_whitespace(client):
    test_client, _ = client
    response = test_client.post("/v1/notes", json={"title": "   "})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_an_unknown_path_answers_in_the_documented_envelope(client):
    """The envelope is every non-2xx answer, including the ones routing raises."""
    test_client, _ = client
    missing = test_client.get("/v1/nothing-here")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"

    wrong_method = test_client.delete("/healthz")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"] == "method_not_allowed"


def test_reading_a_note_returns_the_document_and_its_etag(client):
    test_client, _ = client
    response = test_client.get(f"/v1/notes/{NOTE.id}")
    assert response.status_code == 200
    assert response.headers["etag"] == '"sha256:abc"'
    assert response.json()["id"] == NOTE.id


def test_a_missing_note_is_a_404_in_the_error_envelope(client):
    test_client, fake = client
    fake.error = NotFound("nope")
    response = test_client.get("/v1/notes/nope")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_a_database_outage_write_uses_operation_neutral_wording(client):
    test_client, fake = client
    fake.error = MetadataUnavailable("connection refused")
    response = test_client.post("/v1/notes", json={"title": "During an outage"})
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "metadata_unavailable"
    assert "if this was a write, its outcome is unknown" in body["message"]


def test_a_database_outage_read_uses_operation_neutral_wording(client):
    test_client, fake = client
    fake.error = MetadataUnavailable("connection refused")
    response = test_client.get(f"/v1/notes/{NOTE.id}")
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "metadata_unavailable"
    assert "if this was a write, its outcome is unknown" in body["message"]


def test_a_filesystem_failure_does_not_blame_the_database(client):
    """A read only volume is its own fault, and readiness says the same half."""
    test_client, fake = client
    fake.error = NotesFilesystemUnavailable("[Errno 30] Read-only file system")
    response = test_client.post("/v1/notes", json={"title": "During a remount"})
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "notes_filesystem_unavailable"
    assert "notes filesystem could not be read or written" in body["message"]
    assert "notes filesystem is unaffected" not in body["message"]
    # The public surface has no authentication in this slice, so it must not
    # repeat the operating system's reason, which names container paths.
    assert "detail" not in body
    assert "Read-only file system" not in response.text


def test_an_unreachable_store_reports_an_unknown_in_flight_write(client):
    test_client, fake = client
    fake.error = StoreUnavailable("request timed out")
    response = test_client.post("/v1/notes", json={"title": "During a timeout"})
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "store_unavailable"
    assert "if this was a write, its outcome is unknown" in body["message"]
    assert "notes filesystem is untouched" not in body["message"]


def test_an_unreachable_store_read_uses_operation_neutral_wording(client):
    test_client, fake = client
    fake.error = StoreUnavailable("connection refused")
    response = test_client.get(f"/v1/notes/{NOTE.id}")
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "store_unavailable"
    assert "if this was a write, its outcome is unknown" in body["message"]


def test_a_missing_note_reports_the_identifier_it_was_asked_for(client):
    """The message names the id once, not twice, and the envelope carries it."""
    test_client, fake = client
    fake.error = NotFound(NOTE.id)
    response = test_client.get(f"/v1/notes/{NOTE.id}")
    body = response.json()
    assert body["note_id"] == NOTE.id
    assert body["message"] == f"no note with id {NOTE.id}"


def test_a_schema_violation_comes_back_as_422_with_the_reasons(client):
    test_client, fake = client
    fake.error = ValidationFailed(["account: required when context is one of customer"])
    response = test_client.post("/v1/notes", json={"title": "Missing account"})
    assert response.status_code == 422
    assert response.json()["errors"] == ["account: required when context is one of customer"]


def test_an_unparseable_note_never_returns_the_notes_own_text(client):
    """The public envelope carries none of the broken note's frontmatter.

    The parser's reason quotes the lines it choked on, so it is the caller's
    note content. Two things make publishing it wrong: this surface has no
    authentication in this slice and an operator setting can put it on an
    interface, and the error is raised before the identifier guard can run, so
    a stale mirror row can serve lines from a note the caller never named.

    The assertion is on the absence of that text rather than on the presence of
    a particular sentence, so rewording the message later cannot make this test
    pass while the content leaks again.
    """
    test_client, fake = client
    secret_lines = ["account: AcmeCorp Confidential", "tags: [unclosed", "salary_band: L7"]
    reason = (
        'while parsing a flow sequence\n  in "<unicode string>", line 5, column 7:\n'
        "    tags: [unclosed\n          ^ (line: 5)\n"
        "expected ',' or ']', but got ':'\n"
        '  in "<unicode string>", line 6, column 9:\n    account: AcmeCorp Confidential\n'
    )
    fake.error = NoteUnparseable(NOTE.id, reason)

    response = test_client.get(f"/v1/notes/{NOTE.id}")

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "note_unparseable"
    assert body["note_id"] == NOTE.id
    # Nothing of the file's own content reaches the caller, in any field.
    served = response.text
    for line in secret_lines:
        assert line not in served
    assert "reason" not in body
    assert "unicode string" not in served
