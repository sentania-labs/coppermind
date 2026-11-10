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
    PayloadTooLarge,
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


async def _once(data: bytes):
    yield data


class FakeStore:
    def __init__(self) -> None:
        self.created: list[CreateArtifact] = []
        self.sizes: list[int | None] = []
        self.removed: list[tuple[str, str]] = []
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
        if request.content_base64 and len(request.bytes()) > self.limit:
            raise PayloadTooLarge(self.limit)
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
            content=_once(b"PK\x03\x04"),
        )

    async def remove_artifact(self, artifact_id: str, removal: str = "admin") -> None:
        if artifact_id != ARTIFACT_ID:
            raise ArtifactMissing(artifact_id)
        self.removed.append((artifact_id, removal))

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


def test_put_and_patch_on_an_artifact_are_refused(api):
    """The archive is immutable: a changed file is a new revision through POST."""
    client, fake = api
    for method in ("put", "patch"):
        response = getattr(client, method)(
            f"/v1/artifacts/{ARTIFACT_ID}", headers=_as(fake, "write")
        )
        assert response.status_code == 405, method
        assert response.json()["error"] == "method_not_allowed"
    assert fake.removed == []


def test_an_agent_removes_an_artifact_through_the_api_with_the_write_scope(api):
    """Admin is break glass; everyday removal goes through the API an agent uses."""
    client, fake = api
    for which in ("read", "notes"):
        refused = client.delete(f"/v1/artifacts/{ARTIFACT_ID}", headers=_as(fake, which))
        assert refused.status_code == 403, which
    assert client.delete(f"/v1/artifacts/{ARTIFACT_ID}").status_code == 401
    assert fake.removed == []
    removed = client.delete(f"/v1/artifacts/{ARTIFACT_ID}", headers=_as(fake, "write"))
    assert removed.status_code == 204
    assert removed.content == b""
    assert fake.removed == [(ARTIFACT_ID, "api")]
    missing = client.delete("/v1/artifacts/nope", headers=_as(fake, "write"))
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"


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
    assert body["message"] == OVER_LIMIT
    assert fake.created == []


OVER_LIMIT = (
    "This file is larger than the 10 MiB limit. Files over 10 MiB go through the staged "
    "upload path: reserve with POST /v1/staged, PUT the bytes to the receipt, then "
    "finalize it."
)


def test_an_oversize_file_gets_the_same_plain_refusal_as_multipart_and_as_json(api):
    """One message, whichever shape the upload came in, and whichever check caught it."""
    client, fake = api
    multipart = client.post(
        "/v1/artifacts",
        files={"file": ("big.bin", b"x" * (10 * 1024 * 1024 + 1))},
        data={"kind": "artifact"},
        headers=_as(fake, "write"),
    )
    assert multipart.status_code == 413
    assert multipart.json()["message"] == OVER_LIMIT
    # A JSON body small enough to pass the transport ceiling but over the limit once
    # decoded is caught by the store; the person still reads the same sentence.
    fake.limit = 2048
    body = base64.b64encode(b"x" * 2100).decode()
    through_store = client.post(
        "/v1/artifacts",
        json={"kind": "artifact", "filename": "a.bin", "content_base64": body},
        headers=_as(fake, "write"),
    )
    assert through_store.status_code == 413
    assert through_store.json()["error"] == "payload_too_large"
    assert through_store.json()["limit_bytes"] == 2048
    assert through_store.json()["message"] == OVER_LIMIT.replace("10 MiB", "2 KiB")
    assert "exceeds" not in through_store.json()["message"]
    assert fake.created == []


@pytest.mark.parametrize(
    ("limit", "said"),
    [
        (10 * 1024 * 1024, "10 MiB"),
        (10_000_000, "9.5 MiB"),
        (2048, "2 KiB"),
        (1536, "1.5 KiB"),
        (900, "900 bytes"),
    ],
)
def test_the_limit_is_said_the_way_a_person_reads_it(limit: int, said: str):
    from coppermind_api.v1.artifacts import human_limit

    assert human_limit(limit) == said


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
