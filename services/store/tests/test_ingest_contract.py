"""Ingest across the authenticated internal HTTP contract."""

from datetime import UTC, datetime

import httpx
import pytest
from coppermind_store.auth import InternalAuth
from coppermind_store.internal_api import router
from fastapi import FastAPI

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import (
    CreatedNote,
    CreatedSource,
    IncompleteRevision,
    IngestRequest,
    IngestResult,
    PayloadTooLarge,
    SourceArtifact,
    SourceArtifactDocument,
    SourceClaimMissing,
    SourceManifest,
    SourceRevision,
    SourcesFilesystemUnavailable,
)

TOKEN = "internal-test-token"
REQUEST = IngestRequest.model_validate(
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


async def test_ingest_round_trips_through_the_internal_contract():
    store = FakeStore()
    client = connected(store)
    try:
        assert await client.ingest(REQUEST, payload_size_bytes=10_000) == RESULT
    finally:
        await client.aclose()
    assert store.request == REQUEST
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
            json=REQUEST.model_dump(mode="json", exclude_none=True),
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
            await client.ingest(REQUEST)
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
            json=REQUEST.model_dump(mode="json", exclude_none=True),
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
        result = await client.ingest(REQUEST)
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
            json=REQUEST.model_dump(mode="json", exclude_none=True),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "source_claim_missing"
    assert body["provider"] == "plaud"
    assert body["external_source_id"] == "recording-1"
