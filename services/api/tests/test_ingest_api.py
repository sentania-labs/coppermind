"""The public create-only ingest contract."""

import json
from datetime import UTC, datetime
from pathlib import Path

from coppermind_api.deps import store
from coppermind_api.main import create_app
from fastapi.testclient import TestClient

from coppermind.api_keys import ApiKeySet, create_key
from coppermind.settings import Wiring
from coppermind.store_protocol import (
    CreatedNote,
    CreatedSource,
    IngestRequest,
    IngestResult,
    PayloadTooLarge,
    SourceAlreadyExists,
)

INGEST = {
    "source": {
        "provider": "plaud",
        "external_source_id": "rec_8f3a2c19",
        "source_type": "transcript",
        "artifacts": [{"name": "transcript.txt", "mime_type": "text/plain", "content": "hello"}],
    },
    "note": {"title": "Architecture sync"},
}
RESULT = IngestResult(
    source=CreatedSource(id="01K4Q8Z2A0P1Q2R3S4T5U6V7W8"),
    note=CreatedNote(
        id="01K4Q8Z3N7V2X9M1B5C6D8E0F2",
        path="Review/Architecture sync.md",
    ),
)


class FakeStore:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.ingested: IngestRequest | None = None
        self.payload_size_bytes: int | None = None
        self.ingest_limit_bytes: int | None = None
        created = datetime(2026, 9, 8, tzinfo=UTC)
        self.full_record, self.full_key = create_key(
            "full", ["sources:write", "notes:write"], created_at=created
        )
        self.sources_record, self.sources_key = create_key(
            "sources only", ["sources:write"], created_at=created
        )
        self.notes_record, self.notes_key = create_key(
            "notes only", ["notes:write"], created_at=created
        )

    async def ingest(
        self, request: IngestRequest, *, payload_size_bytes: int | None = None
    ) -> IngestResult:
        if self.error:
            raise self.error
        if (
            self.ingest_limit_bytes is not None
            and (payload_size_bytes or 0) > self.ingest_limit_bytes
        ):
            raise PayloadTooLarge(self.ingest_limit_bytes)
        self.ingested = request
        self.payload_size_bytes = payload_size_bytes
        return RESULT

    async def get_api_keys(self) -> ApiKeySet:
        return ApiKeySet(keys=[self.full_record, self.sources_record, self.notes_record])

    async def is_ready(self) -> bool:
        return True


def client_for(tmp_path: Path):
    token = tmp_path / "internal-token"
    token.write_text("test-token\n", encoding="utf-8")
    app = create_app(Wiring(internal_token_file=token, store_url="http://store.invalid"))
    fake = FakeStore()
    app.dependency_overrides[store] = lambda: fake
    return app, fake


def test_ingest_requires_both_write_scopes(tmp_path: Path):
    app, fake = client_for(tmp_path)
    with TestClient(app) as client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        for key in (fake.sources_key, fake.notes_key):
            response = client.post(
                "/v1/ingest",
                json=INGEST,
                headers={"Authorization": f"Bearer {key}"},
            )
            assert response.status_code == 403
    assert fake.ingested is None


def test_ingest_answers_201_with_the_source_and_linked_note(tmp_path: Path):
    app, fake = client_for(tmp_path)
    with TestClient(app) as client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        response = client.post(
            "/v1/ingest",
            json=INGEST,
            headers={"Authorization": f"Bearer {fake.full_key}"},
        )
    assert response.status_code == 201
    assert response.json() == RESULT.model_dump(mode="json")
    assert fake.ingested is not None
    assert fake.ingested.source.external_source_id == "rec_8f3a2c19"
    assert fake.payload_size_bytes == len(response.request.content)


def test_ingest_limit_uses_the_raw_public_body_size(tmp_path: Path):
    app, fake = client_for(tmp_path)
    compact_size = len(json.dumps(INGEST, separators=(",", ":")).encode("utf-8"))
    raw = json.dumps(INGEST, indent=24).encode("utf-8")
    assert len(raw) > compact_size
    fake.ingest_limit_bytes = compact_size

    with TestClient(app) as client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        response = client.post(
            "/v1/ingest",
            content=raw,
            headers={
                "Authorization": f"Bearer {fake.full_key}",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 413
    assert response.json()["limit_bytes"] == compact_size
    assert fake.ingested is None


def test_duplicate_and_oversize_ingest_keep_the_documented_envelope(tmp_path: Path):
    app, fake = client_for(tmp_path)
    with TestClient(app) as client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        headers = {"Authorization": f"Bearer {fake.full_key}"}

        fake.error = SourceAlreadyExists("plaud", "rec_8f3a2c19")
        duplicate = client.post("/v1/ingest", json=INGEST, headers=headers)
        assert duplicate.status_code == 409
        assert duplicate.json()["error"] == "source_exists"

        fake.error = PayloadTooLarge(1024)
        oversize = client.post("/v1/ingest", json=INGEST, headers=headers)
        assert oversize.status_code == 413
        assert oversize.json()["limit_bytes"] == 1024
