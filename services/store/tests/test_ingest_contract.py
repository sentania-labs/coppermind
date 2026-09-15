"""Ingest across the authenticated internal HTTP contract."""

import httpx
import pytest
from coppermind_store.auth import InternalAuth
from coppermind_store.internal_api import router
from fastapi import FastAPI

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import (
    CreatedNote,
    CreatedSource,
    IngestRequest,
    IngestResult,
    PayloadTooLarge,
    SourceAlreadyExists,
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
    source=CreatedSource(id="01K4Q8Z2A0P1Q2R3S4T5U6V7W8"),
    note=CreatedNote(id="01K4Q8Z3N7V2X9M1B5C6D8E0F2", path="Review/Recording.md"),
)


class FakeStore:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.request: IngestRequest | None = None
        self.payload_size_bytes: int | None = None

    async def ingest(
        self, request: IngestRequest, *, payload_size_bytes: int | None = None
    ) -> IngestResult:
        if self.error:
            raise self.error
        self.request = request
        self.payload_size_bytes = payload_size_bytes
        return RESULT


def connected(store: FakeStore) -> HttpStoreClient:
    app = FastAPI()
    app.state.auth = InternalAuth(TOKEN)
    app.state.store = store
    app.include_router(router)
    return HttpStoreClient("http://store", TOKEN, transport=httpx.ASGITransport(app=app))


async def test_ingest_round_trips_through_the_internal_contract():
    store = FakeStore()
    client = connected(store)
    try:
        assert await client.ingest(REQUEST, payload_size_bytes=10_000) == RESULT
    finally:
        await client.aclose()
    assert store.request == REQUEST
    assert store.payload_size_bytes == 10_000


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SourceAlreadyExists("plaud", "recording-1"), SourceAlreadyExists),
        (PayloadTooLarge(1024), PayloadTooLarge),
        (SourcesFilesystemUnavailable("read only"), SourcesFilesystemUnavailable),
    ],
)
async def test_ingest_errors_keep_their_type_over_http(error, expected):
    store = FakeStore()
    store.error = error
    client = connected(store)
    try:
        with pytest.raises(expected):
            await client.ingest(REQUEST)
    finally:
        await client.aclose()
