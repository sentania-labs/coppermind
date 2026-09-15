"""Read-only source API behavior, including T-SRC-1."""

from contextlib import contextmanager
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
    SourceManifest,
    SourceRevision,
    StoreUnavailable,
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
    def __init__(self, *, reachable: bool = True) -> None:
        self.reachable = reachable
        created = datetime(2026, 9, 8, tzinfo=UTC)
        self.read_record, self.read_key = create_key(
            "source reader", ["sources:read"], created_at=created
        )
        self.other_record, self.other_key = create_key(
            "note reader", ["notes:read"], created_at=created
        )

    async def get_api_keys(self) -> ApiKeySet:
        if not self.reachable:
            raise StoreUnavailable("the store is down")
        return ApiKeySet(keys=[self.read_record, self.other_record])

    async def get_source(self, source_id: str) -> SourceManifest:
        if not self.reachable:
            raise StoreUnavailable("the store is down")
        assert source_id == SOURCE_ID
        return MANIFEST

    async def get_source_artifact(
        self, source_id: str, revision: int, name: str
    ) -> SourceArtifactDocument:
        content = None if name == "recording.bin" else "hello"
        mime_type = "application/octet-stream" if content is None else "text/plain"
        if name == "crafted.txt":
            mime_type = "text/plain\r\nX-Evil: 1"
        return SourceArtifactDocument(
            source_id=source_id,
            revision=revision,
            name=name,
            mime_type=mime_type,
            sha256="abc",
            size_bytes=5,
            content=content,
        )

    async def is_ready(self) -> bool:
        return True


@contextmanager
def _client(tmp_path: Path, *, reachable: bool = True):
    token = tmp_path / "internal-token"
    token.write_text("test-token\n", encoding="utf-8")
    app = create_app(Wiring(internal_token_file=token, store_url="http://store.invalid"))
    fake = FakeStore(reachable=reachable)
    app.dependency_overrides[store] = lambda: fake
    with TestClient(app) as client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        yield client, fake


@pytest.fixture
def source_client(tmp_path: Path):
    with _client(tmp_path) as pair:
        yield pair


def test_a_source_manifest_and_its_text_and_binary_artifacts_are_readable(source_client):
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
    assert text.headers["content-type"] == "text/plain; charset=utf-8"
    assert text.headers["x-content-type-options"] == "nosniff"
    binary = client.get(
        f"/v1/sources/{SOURCE_ID}/revisions/1/artifacts/recording.bin", headers=headers
    )
    assert binary.json()["content"] is None
    assert binary.json()["sha256"] == "abc"


def test_an_ingested_artifact_type_never_reaches_a_response_header(source_client):
    """The declared type is free client text, so this origin does not echo it."""
    client, fake = source_client
    response = client.get(
        f"/v1/sources/{SOURCE_ID}/revisions/1/artifacts/crafted.txt",
        headers={"Authorization": f"Bearer {fake.read_key}"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert "x-evil" not in response.headers
    assert all(
        "\r" not in value and "\n" not in value and value.isascii()
        for value in response.headers.values()
    )


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


def test_t_src_1_a_mutation_alters_nothing_while_the_store_is_unreachable(tmp_path: Path):
    """A source cannot be altered whichever answer the API is able to give.

    The key records live in the Store, so with it down and the cache cold the
    boundary answers 503 before routing decides the method is not allowed. What
    a source read and a source mutation have in common is that neither reaches
    anything that could change source data.
    """
    with _client(tmp_path, reachable=False) as (client, fake):
        headers = {"Authorization": f"Bearer {fake.read_key}"}
        read = client.get(f"/v1/sources/{SOURCE_ID}", headers=headers)
        refused = client.put(f"/v1/sources/{SOURCE_ID}", headers=headers)

    assert read.status_code == 503
    assert refused.status_code == 503
    assert refused.json()["error"] == "store_unavailable"
