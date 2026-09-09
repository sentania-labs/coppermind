"""The internal contract, driven end to end by the client that speaks it.

`LocalStore` and `HttpStoreClient` are interchangeable only if what the store
puts on the wire is what the client reads back off it. These tests run the real
internal router against the real client over an in process transport, so an
error shape that survives one side and not the other fails here rather than in
production.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from coppermind_store import __version__
from coppermind_store.auth import InternalAuth
from coppermind_store.internal_api import router as internal_router
from coppermind_store.main import create_app
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coppermind.settings import Wiring
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import NoteDocument, NotesFilesystemUnavailable, NotFound

TOKEN = "internal-test-token"
NOTE_ID = "01K4Q8Z3N7V2X9M1B5C6D8E0F2"
NOTE = NoteDocument(
    id=NOTE_ID,
    path="Review/Runbook.md",
    title="Runbook",
    frontmatter={"id": NOTE_ID},
    body="# Runbook\n",
    content_hash="sha256:abc",
    size_bytes=11,
    updated_at=datetime(2026, 9, 8, tzinfo=UTC),
)


class RaisingStore:
    """A store that answers every call with the error it was given."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def get_note(self, note_id: str) -> None:
        raise self.error


class OneNoteStore:
    """A store holding exactly one note, so a lookup by any other id misses."""

    def __init__(self, note: NoteDocument) -> None:
        self.note = note
        self.asked_for: list[str] = []

    async def get_note(self, note_id: str) -> NoteDocument:
        self.asked_for.append(note_id)
        if note_id != self.note.id:
            raise NotFound(note_id)
        return self.note


def connected(store: RaisingStore | OneNoteStore) -> HttpStoreClient:
    app = FastAPI()
    app.state.auth = InternalAuth(TOKEN)
    app.state.store = store
    app.include_router(internal_router)
    return HttpStoreClient("http://store", TOKEN, transport=httpx.ASGITransport(app=app))


@pytest.mark.parametrize(
    "suffix",
    ["#anything", "?q=1", " and more"],
    ids=["fragment", "query", "space"],
)
async def test_an_identifier_is_never_reparsed_as_part_of_the_url(suffix: str):
    """An id is one path segment. A `#` or `?` in it used to truncate the lookup.

    The store then answered 200 for the note whose id was the prefix, so the
    public surface returned a note under an identifier nobody asked for.
    """
    store = OneNoteStore(NOTE)
    client = connected(store)
    try:
        with pytest.raises(NotFound):
            await client.get_note(NOTE_ID + suffix)
    finally:
        await client.aclose()
    assert store.asked_for == [NOTE_ID + suffix]


async def test_an_identifier_that_is_only_punctuation_is_a_miss_not_an_outage():
    """`?` alone used to build a URL ending in `/notes/`, which redirects.

    The client could not read that redirect as a typed error, so a healthy
    store answered 503 `store_unavailable` next to a 200 `/readyz`.
    """
    client = connected(OneNoteStore(NOTE))
    try:
        with pytest.raises(NotFound):
            await client.get_note("?")
    finally:
        await client.aclose()


async def test_a_missing_note_comes_back_as_the_same_typed_error():
    """The client rebuilds `NotFound` with the identifier, not with the message."""
    client = connected(RaisingStore(NotFound(NOTE_ID)))
    try:
        with pytest.raises(NotFound) as raised:
            await client.get_note(NOTE_ID)
    finally:
        await client.aclose()
    assert raised.value.note_id == NOTE_ID
    assert str(raised.value) == f"no note with id {NOTE_ID}"


async def test_the_internal_surface_keeps_the_cause_the_public_one_hides():
    """The client rebuilds the typed error with the operating system's reason.

    The same error answers the public API with a fixed message, because that
    surface is unauthenticated and the reason names container paths.
    """
    cause = "[Errno 30] Read-only file system: '/data/notes/Review'"
    client = connected(RaisingStore(NotesFilesystemUnavailable(cause)))
    try:
        with pytest.raises(NotesFilesystemUnavailable) as raised:
            await client.get_note(NOTE_ID)
    finally:
        await client.aclose()
    assert str(raised.value) == cause


def store_app(tmp_path, build_version: str | None = None):
    token = tmp_path / "internal-token"
    token.write_text(f"{TOKEN}\n", encoding="utf-8")
    return create_app(
        Wiring(
            data_dir=tmp_path / "data",
            internal_token_file=token,
            database_url="postgresql://coppermind:coppermind@127.0.0.1:5999/absent",
            build_version=build_version,
        )
    )


def test_an_unknown_internal_route_answers_in_the_documented_envelope(tmp_path):
    """Routing raises these before any route runs, so they bypass a route handler."""
    with TestClient(store_app(tmp_path)) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        missing = client.get("/internal/v1/nothing-here", headers=headers)
        assert missing.status_code == 404
        assert missing.json()["error"] == "not_found"

        wrong_method = client.delete("/internal/v1/notes", headers=headers)
        assert wrong_method.status_code == 405
        assert wrong_method.json()["error"] == "method_not_allowed"


def test_a_malformed_create_body_answers_as_a_validation_error(tmp_path):
    with TestClient(store_app(tmp_path)) as client:
        response = client.post(
            "/internal/v1/notes",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"body": "no title"},
        )
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert body["errors"] == ["title: Field required"]


def test_healthz_reports_the_version_the_image_was_built_from(tmp_path):
    """CI stamps the tag it built from; a working tree run reports the package version."""
    with TestClient(store_app(tmp_path)) as plain:
        assert plain.get("/healthz").json()["version"] == __version__

    with TestClient(store_app(tmp_path, build_version="v1.2.0")) as stamped:
        assert stamped.get("/healthz").json()["version"] == "v1.2.0"
