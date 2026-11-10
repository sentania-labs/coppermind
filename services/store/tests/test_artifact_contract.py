"""Artifacts over the internal contract, and the pure helpers that name and link them.

The HTTP half runs the real internal router against the real client with a
stand-in store, so the bytes route's headers and every typed error survive
the wire in both directions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from coppermind_store.artifacts import (
    artifact_filename,
    body_with_links,
    browse_folder,
    link_for,
    unique_filename,
)
from coppermind_store.auth import InternalAuth
from coppermind_store.dropbox import read_kind_marker
from coppermind_store.internal_api import router as internal_router
from fastapi import FastAPI

from coppermind.settings import ProductSettings
from coppermind.store_client import HttpStoreClient
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
    ValidationFailed,
)

TOKEN = "internal-test-token"
ARTIFACT_ID = "01K4Q8Z2A0P1Q2R3S4T5U6V7W8"
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
RECORD = ArtifactRecord(
    id=ARTIFACT_ID,
    title="VCDX design package",
    origin="api",
    received_at=NOW,
    filename="VCDX design package.pptx",
    mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    sha256="abc",
    size_bytes=3,
    tags=["reference"],
    path="Artifacts/2026/2026-11/VCDX design package.pptx",
    revision=2,
    citing_note_ids=["01K4Q8Z3N7V2X9M1B5C6D8E0F2"],
    text_status="extracted",
    text="Slide 1\nVCDX design package",
    extractor_version="1",
    created_at=NOW,
    updated_at=NOW,
)


async def _once(data: bytes):
    yield data


class ArtifactStore:
    def __init__(self) -> None:
        self.created: list[tuple[CreateArtifact, int | None]] = []
        self.removed: list[str] = []
        self.query: ArtifactQuery | None = None

    async def create_artifact(
        self, request: CreateArtifact, *, payload_size_bytes: int | None = None
    ) -> CreatedArtifact:
        self.created.append((request, payload_size_bytes))
        if request.url == "https://example.test/too-big":
            raise PayloadTooLarge(10)
        return CreatedArtifact(
            id=ARTIFACT_ID, revision=1, created=request.url is None, path=RECORD.path, title="x"
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
        if revision == 9:
            raise ValidationFailed([f"revision: artifact {artifact_id} has no revision 9"])
        return ArtifactBytes(
            id=artifact_id,
            revision=revision or 2,
            filename="Design päckage ✓.pptx",
            mime_type=RECORD.mime_type or "",
            sha256="abc",
            size_bytes=3,
            content=_once(b"\x00\x01\x02"),
        )

    async def list_artifacts(self, query: ArtifactQuery) -> Page[ArtifactSummary]:
        self.query = query
        return Page[ArtifactSummary](
            items=[
                ArtifactSummary.model_validate(
                    RECORD.model_dump(exclude={"text", "revisions", "extractor_version"})
                )
            ],
            next_cursor="more",
        )

    async def remove_artifact(self, artifact_id: str, removal: str = "admin") -> None:
        if artifact_id != ARTIFACT_ID:
            raise ArtifactMissing(artifact_id)
        self.removed.append(f"{artifact_id}:{removal}")


def connected(store: ArtifactStore) -> HttpStoreClient:
    app = FastAPI()
    app.state.auth = InternalAuth(TOKEN)
    app.state.store = store
    app.include_router(internal_router)
    return HttpStoreClient("http://store", TOKEN, transport=httpx.ASGITransport(app=app))


async def test_create_list_get_bytes_and_remove_cross_the_internal_contract():
    store = ArtifactStore()
    client = connected(store)
    try:
        created = await client.create_artifact(
            CreateArtifact(kind="artifact", filename="a.pdf", content_base64="aGk="),
            payload_size_bytes=42,
        )
        assert created.created is True and created.id == ARTIFACT_ID
        assert store.created[0][1] == 42
        replayed = await client.create_artifact(
            CreateArtifact(kind="artifact", url="https://example.test/paper")
        )
        assert replayed.created is False
        with pytest.raises(PayloadTooLarge) as too_large:
            await client.create_artifact(
                CreateArtifact(kind="artifact", url="https://example.test/too-big")
            )
        assert too_large.value.limit_bytes == 10

        query = ArtifactQuery.model_validate(
            {"tag": "reference", "note_id": "n1", "from": date(2026, 11, 1), "limit": 5}
        )
        page = await client.list_artifacts(query)
        assert store.query == query
        assert page.next_cursor == "more"
        assert page.items[0].kind == "artifact"
        assert page.items[0].citing_note_ids == RECORD.citing_note_ids

        record = await client.get_artifact(ARTIFACT_ID)
        assert record == RECORD
        with pytest.raises(ArtifactMissing) as missing:
            await client.get_artifact("nope")
        assert missing.value.artifact_id == "nope"

        data = await client.get_artifact_bytes(ARTIFACT_ID)
        assert b"".join([chunk async for chunk in data.content]) == b"\x00\x01\x02"
        assert data.filename == "Design päckage ✓.pptx"
        assert data.mime_type == RECORD.mime_type
        assert data.revision == 2 and data.sha256 == "abc" and data.size_bytes == 3
        with pytest.raises(ValidationFailed):
            await client.get_artifact_bytes(ARTIFACT_ID, revision=9)

        await client.remove_artifact(ARTIFACT_ID)
        await client.remove_artifact(ARTIFACT_ID, "api")
        assert store.removed == [f"{ARTIFACT_ID}:admin", f"{ARTIFACT_ID}:api"]
        with pytest.raises(ArtifactMissing):
            await client.remove_artifact("gone")
    finally:
        await client.aclose()


def test_browse_folder_is_year_then_month_of_the_received_day_in_the_operator_zone():
    settings = ProductSettings()
    late = datetime(2026, 12, 1, 3, 30, tzinfo=UTC)  # still November 30 in Chicago
    assert browse_folder(late, settings) == "Artifacts/2026/2026-11"
    local = datetime(2026, 11, 4, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
    assert browse_folder(local, settings) == "Artifacts/2026/2026-11"


def test_filenames_keep_their_extension_and_take_a_numbered_suffix_on_collision():
    assert artifact_filename("VCDX: design*package.PPTX") == "VCDX design package.PPTX"
    assert artifact_filename("no-extension") == "no-extension"
    assert artifact_filename("...") == "artifact"
    assert unique_filename("deck.pptx", {"other.pptx"}) == "deck.pptx"
    assert unique_filename("deck.pptx", {"Deck.pptx"}) == "deck (2).pptx"
    assert unique_filename("deck.pptx", {"deck.pptx", "deck (2).pptx"}) == "deck (3).pptx"
    assert unique_filename("README", {"readme"}) == "README (2)"


def test_links_and_the_artifacts_section_are_added_once():
    file_link = link_for("Artifacts/2026/2026-11/deck.pptx", "Deck", None)
    url_link = link_for(None, "A paper", "https://example.test/paper")
    assert file_link == "[[Artifacts/2026/2026-11/deck.pptx]]"
    assert url_link == "[A paper](https://example.test/paper)"
    body = body_with_links("# Note\n\nText.\n", [file_link, url_link])
    assert body == (
        "# Note\n\nText.\n\n## Artifacts\n\n[[Artifacts/2026/2026-11/deck.pptx]]\n"
        "[A paper](https://example.test/paper)\n"
    )
    assert body_with_links(body, [file_link]) == body
    again = body_with_links(body, ["[[Artifacts/2026/2026-11/other.pdf]]"])
    assert again.count("## Artifacts") == 1
    assert again.endswith("[[Artifacts/2026/2026-11/other.pdf]]\n")


def test_a_kind_marker_is_the_word_artifact_or_json_declaring_it():
    assert read_kind_marker(b"artifact\n") == {"kind": "artifact"}
    assert read_kind_marker(b"ARTIFACT") == {"kind": "artifact"}
    assert read_kind_marker(b"note") is None
    assert read_kind_marker(b"") is None
    assert read_kind_marker(
        b'{"kind": "artifact", "title": " Deck ", "tags": ["reference", 3]}'
    ) == {
        "kind": "artifact",
        "title": "Deck",
        "tags": ["reference"],
    }
    assert read_kind_marker(b'{"kind": "note"}') is None
    assert read_kind_marker(b"\xff\xfe") is None


class _Counted(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.pulled = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.pulled += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


async def test_the_client_hands_back_bytes_it_has_not_read_yet():
    body = _Counted([b"a" * 1000, b"b" * 1000, b"c" * 1000])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-Coppermind-Size-Bytes": "3000", "X-Coppermind-SHA256": "abc"},
            stream=body,
        )

    client = HttpStoreClient("http://store", TOKEN, transport=httpx.MockTransport(handler))
    try:
        served = await client.get_artifact_bytes(ARTIFACT_ID)
        assert body.pulled == 0 and served.size_bytes == 3000
        chunks = [chunk async for chunk in served.content]
        assert [len(chunk) for chunk in chunks] == [1000, 1000, 1000]
        assert body.closed
    finally:
        await client.aclose()


async def test_a_refused_download_is_a_typed_error_and_releases_the_connection():
    body = _Counted([b'{"artifact_id": "x"}'])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, headers={"content-type": "application/json"}, stream=body)

    client = HttpStoreClient("http://store", TOKEN, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ArtifactMissing):
            await client.get_artifact_bytes(ARTIFACT_ID)
        assert body.closed
    finally:
        await client.aclose()
