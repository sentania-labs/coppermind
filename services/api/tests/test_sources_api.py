"""Read-only source API behavior, including T-SRC-1."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from coppermind_api.deps import store
from coppermind_api.main import create_app
from fastapi.testclient import TestClient

from coppermind.api_keys import ApiKeySet, create_key
from coppermind.settings import Wiring
from coppermind.store_protocol import (
    SourceArtifact,
    SourceArtifactDocument,
    SourceImmutable,
    SourceManifest,
    SourceProjection,
    SourceRevision,
)

SOURCE_ID = "01K4Q8Z2A0P1Q2R3S4T5U6V7W8"
ARTIFACT = SourceArtifact(name="transcript.txt", mime_type="text/plain", sha256="abc", size_bytes=5)
MANIFEST = SourceManifest(
    schema_version=1,
    source_id=SOURCE_ID,
    provider="plaud",
    external_source_id="recording-1",
    source_type="transcript",
    origin="",
    current_revision=1,
    revisions=[
        SourceRevision(
            revision=1,
            ingested_at=datetime(2026, 9, 8, tzinfo=UTC),
            content_identity="identity",
            artifacts=[ARTIFACT],
        )
    ],
)


class FakeStore:
    def __init__(self) -> None:
        created = datetime(2026, 9, 8, tzinfo=UTC)
        self.read_record, self.read_key = create_key(
            "source reader", ["sources:read"], created_at=created
        )
        self.other_record, self.other_key = create_key(
            "note reader", ["notes:read"], created_at=created
        )

    async def get_api_keys(self) -> ApiKeySet:
        return ApiKeySet(keys=[self.read_record, self.other_record])

    async def get_source(self, source_id: str) -> SourceManifest:
        assert source_id == SOURCE_ID
        return MANIFEST

    async def get_source_artifact(
        self, source_id: str, revision: int, name: str
    ) -> SourceArtifactDocument:
        content = "hello" if name == "transcript.txt" else None
        return SourceArtifactDocument(
            source_id=source_id,
            revision=revision,
            name=name,
            mime_type="text/plain" if content else "application/octet-stream",
            sha256="abc",
            size_bytes=5,
            content=content,
        )

    async def get_source_projection(self, source_id: str) -> SourceProjection:
        return SourceProjection(
            source_id=source_id,
            path="_Sources/Plaud/2026-09-08 Recording.md",
            content="# Recording (source)\n",
        )

    async def refuse_source_mutation(self, source_id: str) -> None:
        raise SourceImmutable(source_id)

    async def is_ready(self) -> bool:
        return True


@pytest.fixture
def source_client(tmp_path: Path):
    token = tmp_path / "internal-token"
    token.write_text("test-token\n", encoding="utf-8")
    app = create_app(Wiring(internal_token_file=token, store_url="http://store.invalid"))
    fake = FakeStore()
    app.dependency_overrides[store] = lambda: fake
    with TestClient(app) as client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        yield client, fake


def test_source_manifest_text_binary_and_projection_are_readable(source_client):
    client, fake = source_client
    headers = {"Authorization": f"Bearer {fake.read_key}"}
    assert client.get(f"/v1/sources/{SOURCE_ID}", headers=headers).json() == MANIFEST.model_dump(
        mode="json"
    )

    text = client.get(
        f"/v1/sources/{SOURCE_ID}/revisions/1/artifacts/transcript.txt", headers=headers
    )
    assert text.text == "hello"
    assert text.headers["x-coppermind-size-bytes"] == "5"
    binary = client.get(
        f"/v1/sources/{SOURCE_ID}/revisions/1/artifacts/recording.bin", headers=headers
    )
    assert binary.json()["content"] is None
    assert binary.json()["sha256"] == "abc"
    projection = client.get(f"/v1/sources/{SOURCE_ID}/projection", headers=headers)
    assert projection.text == "# Recording (source)\n"


def test_source_reads_require_the_source_read_scope(source_client):
    client, fake = source_client
    response = client.get(
        f"/v1/sources/{SOURCE_ID}",
        headers={"Authorization": f"Bearer {fake.other_key}"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
@pytest.mark.parametrize(
    "path",
    [
        f"/v1/sources/{SOURCE_ID}",
        f"/v1/sources/{SOURCE_ID}/revisions/1/artifacts/transcript.txt",
    ],
)
def test_t_src_1_every_source_mutation_is_refused(source_client, method: str, path: str):
    client, fake = source_client
    response = getattr(client, method)(path, headers={"Authorization": f"Bearer {fake.read_key}"})
    assert response.status_code == 405
    assert response.json()["error"] == "method_not_allowed"
