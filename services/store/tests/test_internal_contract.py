"""The internal contract, driven end to end by the client that speaks it.

`LocalStore` and `HttpStoreClient` are interchangeable only if what the store
puts on the wire is what the client reads back off it. These tests run the
real internal router against the real client over an in process transport, so
an encoding that survives one side and not the other fails here rather than in
production.
"""

from __future__ import annotations

import httpx
import pytest
from coppermind_store.auth import InternalAuth
from coppermind_store.internal_api import router as internal_router
from fastapi import FastAPI

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import NotesFilesystemUnavailable, NotFound, RawNote

TOKEN = "internal-test-token"


class OneNoteStore:
    """A store that answers with the note it was given, or raises."""

    def __init__(self, raw: RawNote | None = None, error: Exception | None = None) -> None:
        self.raw = raw
        self.error = error

    async def read_raw(self, note_id: str) -> RawNote:
        if self.error:
            raise self.error
        assert self.raw is not None
        return self.raw


def connected(store: OneNoteStore) -> HttpStoreClient:
    app = FastAPI()
    app.state.auth = InternalAuth(TOKEN)
    app.state.store = store
    app.include_router(internal_router)
    return HttpStoreClient("http://store", TOKEN, transport=httpx.ASGITransport(app=app))


async def test_a_path_outside_latin_1_survives_the_markdown_read():
    """A note titled in Japanese reads back as Markdown with its path intact.

    Header values go on the wire as latin-1, so an unencoded path made the
    store fail while building the response and the caller saw a 503 for a note
    that was there all along.
    """
    raw = RawNote(
        id="01K4Q8Z3N7V2X9M1B5C6D8E0F2",
        path="Review/会議メモ.md",
        text="---\nid: 01K4Q8Z3N7V2X9M1B5C6D8E0F2\n---\n# 会議メモ\n",
        content_hash="sha256:abc",
    )
    client = connected(OneNoteStore(raw=raw))
    try:
        read = await client.read_raw(raw.id)
    finally:
        await client.aclose()
    assert read.path == "Review/会議メモ.md"
    assert read.text == raw.text
    assert read.content_hash == "sha256:abc"


async def test_a_missing_note_comes_back_as_the_same_typed_error():
    """The client rebuilds `NotFound` with the identifier, not with the message."""
    note_id = "01K4Q8Z3N7V2X9M1B5C6D8E0F2"
    client = connected(OneNoteStore(error=NotFound(note_id)))
    try:
        with pytest.raises(NotFound) as raised:
            await client.read_raw(note_id)
    finally:
        await client.aclose()
    assert raised.value.note_id == note_id
    assert str(raised.value) == f"no note with id {note_id}"


async def test_the_internal_surface_keeps_the_cause_the_public_one_hides():
    """The client rebuilds the typed error with the operating system's reason.

    The same error answers the public API with a fixed message, because that
    surface is unauthenticated and the reason names container paths.
    """
    cause = "[Errno 30] Read-only file system: '/data/notes/Review'"
    client = connected(OneNoteStore(error=NotesFilesystemUnavailable(cause)))
    try:
        with pytest.raises(NotesFilesystemUnavailable) as raised:
            await client.read_raw("01K4Q8Z3N7V2X9M1B5C6D8E0F2")
    finally:
        await client.aclose()
    assert str(raised.value) == cause
