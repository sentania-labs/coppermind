"""AC1: the ingest opening note links to its source's _Sources page.
AC2: POST /v1/notes honours the folder parameter and refuses escaping folders.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from coppermind_store.auth import InternalAuth
from coppermind_store.control import ControlState
from coppermind_store.internal_api import router
from coppermind_store.notes import LocalStore
from fastapi import FastAPI

from coppermind.settings import Wiring
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import (
    CreatedNote,
    CreatedSource,
    CreateNote,
    IncompleteRevision,
    IngestRequest,
    IngestResult,
    NoteDocument,
    PayloadTooLarge,
    SourceArtifact,
    SourceArtifactDocument,
    SourceClaimMissing,
    SourceManifest,
    SourceRevision,
    SourcesFilesystemUnavailable,
    StoreError,
    ValidationFailed,
)

TOKEN = "internal-test-token"
INGEST_REQUEST = IngestRequest.model_validate(
    {
        "source": {
            "provider": "plaud",
            "external_source_id": "recording-1",
            "source_type": "transcript",
            "artifacts": [
                {"name": "transcript.txt", "mime_type": "text/plain", "content": "hello"}
            ],
        },
        "note": {"title": "Recording"},
    }
)

RESULT = IngestResult(
    source=CreatedSource(id="01K4Q8Z2A0P1Q2R3S4T5U6V7W8", revision=1, created=True),
    note=CreatedNote(id="01K4Q8Z3N7V2X9M1B5C6D8E0F2", path="Review/Recording.md", created=True),
    projection_path="_Sources/Plaud/Recording.md",
)


class FakeStore:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.result = RESULT
        self.request: IngestRequest | None = None
        self.payload_size_bytes: int | None = None

    async def ingest(
        self, request: IngestRequest, *, payload_size_bytes: int | None = None
    ) -> IngestResult:
        if self.error:
            raise self.error
        self.request = request
        self.payload_size_bytes = payload_size_bytes
        return self.result

    async def get_source(self, source_id: str) -> SourceManifest:
        return SourceManifest(
            schema_version=1,
            source_id=source_id,
            provider="plaud",
            external_source_id="recording-1",
            source_type="transcript",
            origin="",
            current_revision=1,
            revisions=[
                SourceRevision(
                    revision=1,
                    ingested_at=datetime(2026, 9, 8, 19, 2, 11, tzinfo=UTC),
                    content_identity="identity",
                    artifacts=[
                        SourceArtifact(
                            name="transcript.txt",
                            mime_type="text/plain",
                            sha256="abc",
                            size_bytes=5,
                        )
                    ],
                )
            ],
        )

    async def get_source_artifact(
        self, source_id: str, revision: int, name: str
    ) -> SourceArtifactDocument:
        return SourceArtifactDocument(
            source_id=source_id,
            revision=revision,
            name=name,
            mime_type="text/plain",
            sha256="abc",
            size_bytes=5,
            content="hello",
        )


def app_for(store: FakeStore) -> FastAPI:
    app = FastAPI()
    app.state.auth = InternalAuth(TOKEN)
    app.state.store = store
    app.include_router(router)
    return app


def connected(store: FakeStore) -> HttpStoreClient:
    return HttpStoreClient("http://store", TOKEN, transport=httpx.ASGITransport(app=app_for(store)))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _store(tmp_path: Path):
    """Create a LocalStore backed by *tmp_path* with control defaults."""
    wiring = Wiring(data_dir=tmp_path)
    notes_dir = tmp_path / "notes"
    sources_dir = tmp_path / "sources"
    state_dir = tmp_path / "state"
    notes_dir.mkdir()
    sources_dir.mkdir()
    control = ControlState(state_dir)
    control.ensure_defaults()
    store = LocalStore(notes_dir, control, cast(Any, object()), sources_dir)
    try:
        yield store
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# AC1: the ingest opening note contains a wikilink to the projection page
# ---------------------------------------------------------------------------


async def test_ingest_opening_note_contains_wikilink_to_projection(tmp_path: Path):
    """The body of the note created by ingest includes a wikilink to the
    projection file under _Sources."""
    from coppermind import frontmatter as fm

    async with _store(tmp_path) as store:
        result = await store.ingest(INGEST_REQUEST)
        assert result.note.created
        assert result.projection_path.startswith("_Sources/")

        # Read the note file from disk
        note_path = tmp_path / "notes" / result.note.path
        assert note_path.exists()

        raw_text = note_path.read_text()
        # The body should contain the wikilink referencing the projection stem
        projection_stem = Path(result.projection_path).stem
        assert f"[[{projection_stem}]]" in raw_text
        # The frontmatter sources key must still carry the source ULID
        frontmatter_block, _body_text = fm.split(raw_text)
        loaded = fm._yaml().load(frontmatter_block)
        assert isinstance(loaded["sources"], list)
        assert len(loaded["sources"]) >= 1


# ---------------------------------------------------------------------------
# AC2: POST /v1/notes honours folder and refuses escaping folders
# ---------------------------------------------------------------------------


async def test_create_note_with_folder(tmp_path: Path):
    """A CreateNote with folder lands the note in that folder."""
    async with _store(tmp_path) as store:
        note = await store.create_note(
            CreateNote(title="Test in folder", frontmatter={}, folder="Work/Customers")
        )
        assert note.path == "Work/Customers/Test in folder.md"


async def test_create_note_with_nested_folder(tmp_path: Path):
    """A nested folder path is sanitized and resolved correctly."""
    async with _store(tmp_path) as store:
        note = await store.create_note(
            CreateNote(title="Deep note", frontmatter={}, folder="A/B/C")
        )
        assert note.path == "A/B/C/Deep note.md"


async def test_create_note_folder_refuses_sources_root(tmp_path: Path):
    """A folder that is _Sources itself is refused with 422."""
    async with _store(tmp_path) as store:
        with pytest.raises(ValidationFailed) as exc_info:
            await store.create_note(
                CreateNote(title="Bad folder", frontmatter={}, folder="_Sources")
            )
        assert any("_Sources" in str(err) for err in exc_info.value.errors)


async def test_create_note_folder_refuses_inside_sources(tmp_path: Path):
    """A folder inside _Sources is refused with 422."""
    async with _store(tmp_path) as store:
        with pytest.raises(ValidationFailed) as exc_info:
            await store.create_note(
                CreateNote(title="Bad folder", frontmatter={}, folder="_Sources/Plaud")
            )
        assert any("_Sources" in str(err) for err in exc_info.value.errors)


async def test_create_note_folder_refuses_path_escape(tmp_path: Path):
    """A folder with .. that escapes the notes root is refused."""
    async with _store(tmp_path) as store:
        with pytest.raises(ValidationFailed) as exc_info:
            await store.create_note(
                CreateNote(title="Escape", frontmatter={}, folder="../outside")
            )
        assert any("escapes" in str(err).lower() for err in exc_info.value.errors)


async def test_create_note_absent_folder_uses_review(tmp_path: Path):
    """An absent folder field defaults to the review folder."""
    async with _store(tmp_path) as store:
        note = await store.create_note(CreateNote(title="Review note", frontmatter={}))
        assert note.path.startswith("Review/")


# ---------------------------------------------------------------------------
# HTTP contract round-trip for ingest
# ---------------------------------------------------------------------------


async def test_ingest_round_trips_through_the_internal_contract():
    store = FakeStore()
    client = connected(store)
    try:
        assert await client.ingest(INGEST_REQUEST, payload_size_bytes=10_000) == RESULT
    finally:
        await client.aclose()
    assert store.request == INGEST_REQUEST
    assert store.payload_size_bytes == 10_000


async def test_replay_answers_200_over_the_internal_contract():
    store = FakeStore()
    store.result = RESULT.model_copy(
        update={
            "source": RESULT.source.model_copy(update={"created": False}),
            "note": RESULT.note.model_copy(update={"created": False}),
        }
    )
    async with httpx.AsyncClient(
        base_url="http://store", transport=httpx.ASGITransport(app=app_for(store))
    ) as client:
        response = await client.post(
            "/internal/v1/ingest",
            json=INGEST_REQUEST.model_dump(mode="json", exclude_none=True),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 200
    assert response.json()["source"]["created"] is False


async def test_source_reads_round_trip_through_the_contract():
    store = FakeStore()
    client = connected(store)
    try:
        assert (await client.get_source(RESULT.source.id)).current_revision == 1
        artifact = await client.get_source_artifact(RESULT.source.id, 1, "transcript.txt")
        assert artifact.content == "hello"
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (PayloadTooLarge(1024), PayloadTooLarge),
        (SourcesFilesystemUnavailable("read only"), SourcesFilesystemUnavailable),
        (SourceClaimMissing("plaud", "recording-1"), SourceClaimMissing),
        (IncompleteRevision("01K4Q8Z2A0P1Q2R3S4T5U6V7W8/r0002"), IncompleteRevision),
    ],
)
async def test_ingest_errors_keep_their_type_over_http(error, expected):
    store = FakeStore()
    store.error = error
    client = connected(store)
    try:
        with pytest.raises(expected) as raised:
            await client.ingest(INGEST_REQUEST)
    finally:
        await client.aclose()
    assert str(raised.value) == str(error)


async def test_a_leftover_revision_directory_answers_409_naming_it():
    """An interrupted revision write is a conflict an operator can act on, never a 503."""
    store = FakeStore()
    store.error = IncompleteRevision("01K4Q8Z2A0P1Q2R3S4T5U6V7W8/r0002")
    async with httpx.AsyncClient(
        base_url="http://store", transport=httpx.ASGITransport(app=app_for(store))
    ) as client:
        response = await client.post(
            "/internal/v1/ingest",
            json=INGEST_REQUEST.model_dump(mode="json", exclude_none=True),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "incomplete_revision"
    assert body["path"] == "01K4Q8Z2A0P1Q2R3S4T5U6V7W8/r0002"


async def test_unstored_fields_survive_the_internal_contract():
    """The caller learns what was not stored whether it holds a store or a client."""
    store = FakeStore()
    store.result = RESULT.model_copy(
        update={
            "source": RESULT.source.model_copy(
                update={"created": False, "unstored_fields": ["captured_at", "mime_type"]}
            ),
            "note": RESULT.note.model_copy(update={"created": False}),
        }
    )
    client = connected(store)
    try:
        result = await client.ingest(INGEST_REQUEST)
    finally:
        await client.aclose()
    assert result.source.unstored_fields == ["captured_at", "mime_type"]


async def test_a_missing_claim_answers_409_naming_the_external_id():
    store = FakeStore()
    store.error = SourceClaimMissing("plaud", "recording-1")
    async with httpx.AsyncClient(
        base_url="http://store", transport=httpx.ASGITransport(app=app_for(store))
    ) as client:
        response = await client.post(
            "/internal/v1/ingest",
            json=INGEST_REQUEST.model_dump(mode="json", exclude_none=True),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "source_claim_missing"
    assert body["provider"] == "plaud"
    assert body["external_source_id"] == "recording-1"
