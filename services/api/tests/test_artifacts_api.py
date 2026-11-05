"""`/v1/artifacts`: scopes, both upload shapes, the declared-kind refusal and the bytes route."""

from __future__ import annotations

import base64
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
    ArtifactBytes,
    ArtifactMissing,
    ArtifactQuery,
    ArtifactRecord,
    ArtifactSummary,
    CreateArtifact,
    CreatedArtifact,
    Page,
)

ARTIFACT_ID = "01K4Q8Z2A0P1Q2R3S4T5U6V7W8"
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
RECORD = ArtifactRecord(
    id=ARTIFACT_ID,
    title="Deck",
    origin="api",
    received_at=NOW,
    filename="deck.pptx",
    mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    sha256="abc",
    size_bytes=4,
    path="Artifacts/2026/2026-11/deck.pptx",
    text="Slide 1\nDeck",
    text_status="extracted",
    extractor_version="1",
    created_at=NOW,
    updated_at=NOW,
)


class FakeStore:
    def __init__(self) -> None:
        self.created: list[CreateArtifact] = []
        self.sizes: list[int | None] = []
        self.limit = 10 * 1024 * 1024
        created = datetime(2026, 9, 8, tzinfo=UTC)
        self.write_record, self.write_key = create_key(
            "writer", ["artifacts:write"], created_at=created
        )
        self.read_record, self.read_key = create_key(
            "reader", ["artifacts:read"], created_at=created
        )
        self.notes_record, self.notes_key = create_key(
            "notes", ["notes:read", "notes:write"], created_at=created
        )

    async def get_api_keys(self) -> ApiKeySet:
        return ApiKeySet(keys=[self.write_record, self.read_record, self.notes_record])

    async def create_artifact(
        self, request: CreateArtifact, *, payload_size_bytes: int | None = None
    ) -> CreatedArtifact:
        self.created.append(request)
        self.sizes.append(payload_size_bytes)
        return CreatedArtifact(
            id=ARTIFACT_ID,
            revision=1,
            created=request.url is None,
            path=RECORD.path,
            title=request.title or "Deck",
            note_id=request.note_id,
        )

    async def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        if artifact_id != ARTIFACT_ID:
            raise ArtifactMissing(artifact_id)
        return RECORD

    async def get_artifact_bytes(
        self, artifact_id: str, revision: int | None = None
    ) -> ArtifactBytes:
        if artifact_id != ARTIFACT_ID:
            raise ArtifactMissing(artifact_id)
        return ArtifactBytes(
            id=artifact_id,
            revision=revision or 1,
            filename="dëck.pptx" if revision is None else "crafted\r\nname.pptx",
            mime_type=(RECORD.mime_type or "") if revision is None else "text/plain\r\nX-Evil: 1",
            sha256="abc",
            size_bytes=4,
            content=b"PK\x03\x04",
        )

    async def list_artifacts(self, query: ArtifactQuery) -> Page[ArtifactSummary]:
        self.query = query
        summary = ArtifactSummary.model_validate(
            RECORD.model_dump(exclude={"text", "revisions", "extractor_version"})
        )
        return Page[ArtifactSummary](items=[summary], next_cursor=None)

    async def get_artifact_limit(self) -> int:
        return self.limit

    async def is_ready(self) -> bool:
        return True


@contextmanager
def _client(tmp_path: Path):
    token = tmp_path / "internal-token"
    token.write_text("test-token\n", encoding="utf-8")
    app = create_app(Wiring(internal_token_file=token, store_url="http://store.invalid"))
    fake = FakeStore()
    app.dependency_overrides[store] = lambda: fake
    with TestClient(app) as client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        yield client, fake


@pytest.fixture
def api(tmp_path: Path):
    with _client(tmp_path) as pair:
        yield pair


def _as(fake: FakeStore, which: str) -> dict[str, str]:
    key = {"write": fake.write_key, "read": fake.read_key, "notes": fake.notes_key}[which]
    return {"Authorization": f"Bearer {key}"}


def test_artifact_routes_need_their_own_scopes(api):
    client, fake = api
    assert client.post("/v1/artifacts", json={"kind": "artifact"}).status_code == 401
    for which in ("read", "notes"):
        refused = client.post(
            "/v1/artifacts",
            json={"kind": "artifact", "url": "https://example.test/a"},
            headers=_as(fake, which),
        )
        assert refused.status_code == 403, which
    for which in ("write", "notes"):
        assert client.get("/v1/artifacts", headers=_as(fake, which)).status_code == 403
        assert (
            client.get(f"/v1/artifacts/{ARTIFACT_ID}", headers=_as(fake, which)).status_code == 403
        )
    assert fake.created == []


def test_a_multipart_upload_becomes_a_declared_artifact(api):
    client, fake = api
    response = client.post(
        "/v1/artifacts",
        files={"file": ("VCDX design.pptx", b"PK\x03\x04deck", "application/vnd.ms-powerpoint")},
        data={
            "kind": "artifact",
            "title": "VCDX design package",
            "date": "2026-11-04",
            "tags": "reference, vcdx",
            "note_id": "01K4Q8Z3N7V2X9M1B5C6D8E0F2",
        },
        headers=_as(fake, "write"),
    )
    assert response.status_code == 201, response.text
    assert response.headers["location"] == f"/v1/artifacts/{ARTIFACT_ID}"
    body = response.json()
    assert body["kind"] == "artifact" and body["created"] is True
    assert body["path"] == RECORD.path
    assert body["note_id"] == "01K4Q8Z3N7V2X9M1B5C6D8E0F2"
    sent = fake.created[0]
    assert sent.filename == "VCDX design.pptx"
    assert sent.bytes() == b"PK\x03\x04deck"
    assert sent.mime_type == "application/vnd.ms-powerpoint"
    assert sent.tags == ["reference", "vcdx"]
    assert sent.date.isoformat() == "2026-11-04"
    assert sent.title == "VCDX design package"
    assert fake.sizes == [len(b"PK\x03\x04deck")]


def test_a_json_link_is_an_artifact_and_a_replay_answers_200(api):
    client, fake = api
    response = client.post(
        "/v1/artifacts",
        json={"kind": "artifact", "url": "https://example.test/paper", "title": "A paper"},
        headers=_as(fake, "write"),
    )
    assert response.status_code == 200
    assert response.json()["created"] is False
    assert fake.created[0].url == "https://example.test/paper"


def test_an_upload_without_a_kind_or_with_a_note_kind_is_refused_with_a_reason(api):
    client, fake = api
    encoded = base64.b64encode(b"hi").decode()
    missing = client.post(
        "/v1/artifacts",
        json={"filename": "deck.pptx", "content_base64": encoded},
        headers=_as(fake, "write"),
    )
    assert missing.status_code == 422
    assert missing.json()["error"] == "validation_error"
    assert "kind: a declared kind is required" in missing.json()["message"]
    wrong = client.post(
        "/v1/artifacts",
        files={"file": ("deck.pptx", b"hi")},
        data={"kind": "note"},
        headers=_as(fake, "write"),
    )
    assert wrong.status_code == 422
    assert "only artifact can be stored here" in wrong.json()["message"]
    assert fake.created == []


def test_a_body_that_is_neither_json_nor_multipart_is_refused(api):
    client, fake = api
    response = client.post(
        "/v1/artifacts",
        content=b"not json",
        headers={**_as(fake, "write"), "Content-Type": "text/plain"},
    )
    assert response.status_code == 422
    assert "must be JSON or multipart" in response.json()["message"]


def test_the_record_and_the_listing_carry_the_kind(api):
    client, fake = api
    record = client.get(f"/v1/artifacts/{ARTIFACT_ID}", headers=_as(fake, "read"))
    assert record.status_code == 200
    assert record.json()["kind"] == "artifact"
    assert record.json()["text"] == "Slide 1\nDeck"
    assert client.get("/v1/artifacts/nope", headers=_as(fake, "read")).status_code == 404
    listing = client.get(
        "/v1/artifacts",
        params={"tag": "reference", "note_id": "n1", "from": "2026-11-01"},
        headers=_as(fake, "read"),
    )
    assert listing.status_code == 200
    assert listing.json()["items"][0]["kind"] == "artifact"
    assert fake.query.tag == "reference" and fake.query.note_id == "n1"
    assert fake.query.from_date.isoformat() == "2026-11-01"


def test_bytes_are_served_with_their_type_and_a_safe_disposition(api):
    client, fake = api
    response = client.get(f"/v1/artifacts/{ARTIFACT_ID}/bytes", headers=_as(fake, "read"))
    assert response.status_code == 200
    assert response.content == b"PK\x03\x04"
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-coppermind-sha256"] == "abc"
    assert "filename*=UTF-8''d%C3%ABck.pptx" in response.headers["content-disposition"]
    assert 'filename="dck.pptx"' in response.headers["content-disposition"]


def test_a_recorded_type_never_reaches_a_response_header(api):
    client, fake = api
    response = client.get(
        f"/v1/artifacts/{ARTIFACT_ID}/bytes", params={"revision": 2}, headers=_as(fake, "read")
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert "x-evil" not in response.headers
    assert all(
        "\r" not in value and "\n" not in value and value.isascii()
        for value in response.headers.values()
    )


def test_mutating_verbs_on_an_artifact_are_refused(api):
    client, fake = api
    for method in ("put", "patch", "delete"):
        response = getattr(client, method)(
            f"/v1/artifacts/{ARTIFACT_ID}", headers=_as(fake, "write")
        )
        assert response.status_code == 405, method
        assert response.json()["error"] == "method_not_allowed"


def test_an_upload_that_announces_an_impossible_size_is_refused_before_parsing(api):
    client, fake = api
    response = client.post(
        "/v1/artifacts",
        content=b"{}",
        headers={
            **_as(fake, "write"),
            "Content-Type": "application/json",
            "Content-Length": str(40_000_000),
        },
    )
    assert response.status_code == 413
    body = response.json()
    assert body["error"] == "payload_too_large"
    assert body["limit_bytes"] == 10 * 1024 * 1024
    assert "staged upload path" in body["message"]
    assert fake.created == []


def test_a_file_part_over_the_ceiling_is_refused(api):
    client, fake = api
    # The ceiling comes from the store's setting and leaves room for multipart
    # framing, so the exact check is on the decoded bytes: one byte over is
    # refused, the ceiling itself is accepted.
    fake.limit = 1024
    response = client.post(
        "/v1/artifacts",
        files={"file": ("big.bin", b"x" * 1025)},
        data={"kind": "artifact"},
        headers=_as(fake, "write"),
    )
    assert response.status_code == 413
    assert fake.created == []
    small = client.post(
        "/v1/artifacts",
        files={"file": ("small.bin", b"x" * 1024)},
        data={"kind": "artifact"},
        headers=_as(fake, "write"),
    )
    assert small.status_code == 201


def test_the_ceiling_follows_the_store_setting_and_refuses_before_buffering(api):
    client, fake = api
    fake.limit = 2048
    response = client.post(
        "/v1/artifacts",
        content=b"{}",
        headers={
            **_as(fake, "write"),
            "Content-Type": "application/json",
            "Content-Length": str(1_000_000),
        },
    )
    assert response.status_code == 413
    assert response.json()["limit_bytes"] == 2048
    assert fake.created == []
    fake.limit = 100 * 1024 * 1024
    larger = client.post(
        "/v1/artifacts",
        files={"file": ("big.bin", b"x" * (11 * 1024 * 1024))},
        data={"kind": "artifact"},
        headers=_as(fake, "write"),
    )
    assert larger.status_code == 201
    assert len(fake.created) == 1


def test_a_file_sent_as_base64_is_measured_on_its_decoded_bytes(api):
    client, fake = api
    fake.limit = 3000
    inside = base64.b64encode(b"x" * 3000).decode()
    response = client.post(
        "/v1/artifacts",
        json={"kind": "artifact", "filename": "a.bin", "content_base64": inside},
        headers=_as(fake, "write"),
    )
    assert response.status_code == 201, response.text
    assert fake.sizes == [None]
    outside = base64.b64encode(b"x" * 100_000).decode()
    refused = client.post(
        "/v1/artifacts",
        json={"kind": "artifact", "filename": "b.bin", "content_base64": outside},
        headers=_as(fake, "write"),
    )
    assert refused.status_code == 413
    assert len(fake.created) == 1


def test_a_multipart_upload_without_a_content_length_stops_being_read_at_the_ceiling(api):
    import asyncio

    client, fake = api
    fake.limit = 1024 * 1024
    boundary = "bound"
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="kind"\r\n\r\nartifact\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        'filename="big.bin"\r\nContent-Type: application/octet-stream\r\n\r\n'
    ).encode()
    delivered = 0
    chunk = b"x" * (1024 * 1024)

    async def receive():
        nonlocal delivered
        delivered += 1
        body = head if delivered == 1 else chunk
        return {"type": "http.request", "body": body, "more_body": delivered < 200}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/artifacts",
        "raw_path": b"/v1/artifacts",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"authorization", _as(fake, "write")["Authorization"].encode()),
            (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
        "app": client.app,
    }
    asyncio.run(client.app(scope, receive, send))

    assert next(m for m in sent if m["type"] == "http.response.start")["status"] == 413
    assert delivered <= 4
    assert fake.created == []
