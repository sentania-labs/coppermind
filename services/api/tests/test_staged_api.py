"""`/v1/staged`: the three-request path for a file over the inline limit.

The HTTP half runs the real routes against a stand-in store, so the scopes, the
declared-size refusal, the streamed PUT's byte count and every typed error
survive the wire in both directions. The store-side behaviour of the same
receipt is in `services/store/tests/test_staged.py`.
"""

from __future__ import annotations

import hashlib
import json
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
    CreatedArtifact,
    ReceiptMissing,
    StagedLimits,
    StagedReceipt,
    StagedReservation,
    ValidationFailed,
)

ARTIFACT_ID = "01K4Q8Z2A0P1Q2R3S4T5U6V7W8"
RECEIPT_ID = "01K4Q8Z2A0P1Q2R3S4T5U6V7W9"
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
LIMITS = StagedLimits(inline_max_bytes=10 * 1024 * 1024, staged_max_bytes=50 * 1024 * 1024)
DIGEST = hashlib.sha256(b"quokka").hexdigest()

ARTIFACT = CreatedArtifact(
    id=ARTIFACT_ID,
    revision=1,
    created=True,
    path="Artifacts/2026/2026-11/deck.pptx",
    title="Deck",
)


def _receipt(state="uploading", upload_bytes=0, artifact=None, failure=None) -> StagedReceipt:
    return StagedReceipt(
        id=RECEIPT_ID,
        state=state,
        filename="deck.pptx",
        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        size_bytes=6,
        sha256=DIGEST,
        upload_bytes=upload_bytes,
        limits=LIMITS,
        failure=failure,
        artifact=artifact,
        reserved_at=NOW,
        updated_at=NOW,
    )


class FakeStore:
    def __init__(self) -> None:
        self.reserved: list[StagedReservation] = []
        self.uploads: list[tuple[str, bytes, int | None]] = []
        self.finalized: list[str] = []
        self.receipt = _receipt()
        self.write_record, self.write_key = create_key(
            "writer", ["artifacts:write"], created_at=NOW
        )
        self.read_record, self.read_key = create_key("reader", ["artifacts:read"], created_at=NOW)

    async def get_api_keys(self) -> ApiKeySet:
        return ApiKeySet(keys=[self.write_record, self.read_record])

    async def reserve_staged_upload(self, request: StagedReservation) -> StagedReceipt:
        self.reserved.append(request)
        if request.size_bytes > LIMITS.staged_max_bytes:
            raise ValidationFailed(
                [
                    "size_bytes: above the staged maximum of "
                    f"{LIMITS.staged_max_bytes} bytes; split or compress the file"
                ]
            )
        return self.receipt

    async def put_staged_bytes(
        self, receipt_id: str, data: bytes, *, declared_bytes: int | None = None
    ) -> StagedReceipt:
        self.uploads.append((receipt_id, data, declared_bytes))
        self.receipt = _receipt(state="uploading", upload_bytes=len(data))
        return self.receipt

    async def finalize_staged_upload(self, receipt_id: str) -> StagedReceipt:
        self.finalized.append(receipt_id)
        self.receipt = _receipt(state="stored", upload_bytes=6, artifact=ARTIFACT)
        return self.receipt

    async def get_staged_upload(self, receipt_id: str) -> StagedReceipt:
        if receipt_id != RECEIPT_ID:
            raise ReceiptMissing(receipt_id)
        return self.receipt

    async def get_artifact_limit(self) -> int:
        return LIMITS.inline_max_bytes

    async def is_ready(self) -> bool:
        return True


def _as(fake: FakeStore, which: str = "write") -> dict[str, str]:
    key = {"write": fake.write_key, "read": fake.read_key}[which]
    return {"Authorization": f"Bearer {key}"}


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
def api(tmp_path):
    with _client(tmp_path) as pair:
        yield pair


DECLARATION = {
    "filename": "deck.pptx",
    "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "size_bytes": 6,
    "sha256": DIGEST,
    "fields": {"title": "Deck", "tags": ["reference"], "origin": "n8n"},
}


def test_a_reservation_returns_the_receipt_and_the_accepted_limits(api):
    client, fake = api
    response = client.post("/v1/staged", json=DECLARATION, headers=_as(fake, "write"))
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == RECEIPT_ID
    assert body["state"] == "uploading"
    assert body["limits"] == {
        "inline_max_bytes": 10 * 1024 * 1024,
        "staged_max_bytes": 50 * 1024 * 1024,
    }
    assert fake.reserved[0].fields.title == "Deck"
    assert fake.reserved[0].fields.tags == ["reference"]
    assert fake.reserved[0].size_bytes == 6
    assert fake.reserved[0].filename == "deck.pptx"
    assert fake.reserved[0].sha256 == DIGEST


def test_the_write_routes_need_the_write_scope_and_the_read_the_read_scope(api):
    client, fake = api
    assert client.post("/v1/staged", json=DECLARATION).status_code == 401
    assert client.put(f"/v1/staged/{RECEIPT_ID}", content=b"quokka").status_code == 401
    assert client.post(f"/v1/staged/{RECEIPT_ID}/finalize").status_code == 401
    assert client.get(f"/v1/staged/{RECEIPT_ID}").status_code == 401
    assert client.post("/v1/staged", json=DECLARATION, headers=_as(fake, "read")).status_code == 403
    assert (
        client.put(f"/v1/staged/{RECEIPT_ID}", content=b"x", headers=_as(fake, "read")).status_code
        == 403
    )
    assert (
        client.post(f"/v1/staged/{RECEIPT_ID}/finalize", headers=_as(fake, "read")).status_code
        == 403
    )
    # A write key holds only `artifacts:write`, so it cannot read a receipt.
    assert client.get(f"/v1/staged/{RECEIPT_ID}", headers=_as(fake, "write")).status_code == 403
    assert client.get(f"/v1/staged/{RECEIPT_ID}", headers=_as(fake, "read")).status_code == 200


def test_an_oversize_declaration_is_refused_with_the_staged_maximum(api):
    client, fake = api
    too_big = dict(DECLARATION, size_bytes=50 * 1024 * 1024 + 1)
    response = client.post("/v1/staged", json=too_big, headers=_as(fake, "write"))
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert "staged maximum" in body["message"]
    assert fake.reserved == []


def test_a_declaration_without_a_hash_or_a_size_is_refused_with_the_reason(api):
    client, fake = api
    missing = client.post("/v1/staged", json={"filename": "deck.pptx"}, headers=_as(fake, "write"))
    assert missing.status_code == 422
    body = missing.json()
    assert body["error"] == "validation_error"
    assert any("size_bytes" in problem for problem in body["errors"])
    bad_hash = client.post(
        "/v1/staged", json=dict(DECLARATION, sha256="quokka"), headers=_as(fake, "write")
    )
    assert bad_hash.status_code == 422


def test_a_put_streams_the_bytes_and_reports_the_verified_count(api):
    client, fake = api
    response = client.put(
        f"/v1/staged/{RECEIPT_ID}",
        content=b"quokka",
        headers={**_as(fake, "write"), "Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200
    assert response.json()["upload_bytes"] == 6
    assert fake.uploads == [(RECEIPT_ID, b"quokka", 6)]


def test_finalize_answers_with_the_stored_artifact(api):
    client, fake = api
    response = client.post(f"/v1/staged/{RECEIPT_ID}/finalize", headers=_as(fake, "write"))
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "stored"
    assert body["artifact"]["id"] == ARTIFACT_ID
    assert body["artifact"]["path"] == "Artifacts/2026/2026-11/deck.pptx"
    assert fake.finalized == [RECEIPT_ID]


def test_an_unknown_receipt_is_404_on_every_route(api):
    client, fake = api

    async def missing(receipt_id: str) -> StagedReceipt:
        raise ReceiptMissing(receipt_id)

    fake.get_staged_upload = missing
    fake.finalize_staged_upload = missing

    async def missing_put(receipt_id, data, *, declared_bytes=None):
        raise ReceiptMissing(receipt_id)

    fake.put_staged_bytes = missing_put
    assert client.get("/v1/staged/nope", headers=_as(fake, "read")).status_code == 404
    assert client.post("/v1/staged/nope/finalize", headers=_as(fake, "write")).status_code == 404
    assert (
        client.put("/v1/staged/nope", content=b"x", headers=_as(fake, "write")).status_code == 404
    )


def test_a_failed_upload_is_reported_without_becoming_an_artifact(api):
    client, fake = api

    async def mismatch(receipt_id, data, *, declared_bytes=None):
        fake.uploads.append((receipt_id, data, declared_bytes))
        return _receipt(state="failed", failure="mismatch")

    fake.put_staged_bytes = mismatch
    response = client.put(f"/v1/staged/{RECEIPT_ID}", content=b"quokka", headers=_as(fake, "write"))
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "failed"
    assert body["artifact"] is None
    assert body["failure"] == "mismatch"


def test_a_put_past_the_staged_maximum_is_refused_before_it_reaches_the_store(api, monkeypatch):
    from coppermind_api.v1 import staged as staged_routes

    monkeypatch.setattr(staged_routes, "settings_limit", lambda: 8)
    client, fake = api
    headers = {**_as(fake, "write"), "Content-Type": "application/octet-stream"}
    sized = client.put(f"/v1/staged/{RECEIPT_ID}", content=b"123456789", headers=headers)
    assert sized.status_code == 413
    assert sized.json()["error"] == "payload_too_large"

    def chunks():
        yield b"12345"
        yield b"6789"

    chunked = client.put(f"/v1/staged/{RECEIPT_ID}", content=chunks(), headers=headers)
    assert chunked.status_code == 413
    assert fake.uploads == []


def test_a_declaration_far_larger_than_its_framing_is_refused_unparsed(api):
    client, fake = api
    huge = json.dumps(dict(DECLARATION, fields={"title": "x" * (128 * 1024)}))
    response = client.post(
        "/v1/staged",
        content=huge,
        headers={**_as(fake, "write"), "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert fake.reserved == []
