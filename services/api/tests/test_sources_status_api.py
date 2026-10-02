"""`GET /v1/sources`, `GET /v1/notes/{id}/sources` and `GET /v1/status`.

The API keeps no state of its own, so, as in the other API tests, the store is
replaced by an object that satisfies the same contract. This one keeps notes
and sources in memory, so a note created through `POST /v1/notes` or a source
ingested through `POST /v1/ingest` shows up in the listings and counters the
routes under test return. What is under test is the public mapping: routes,
scopes, query parameters, paging and the error envelope. The counting itself
runs against PostgreSQL in tests/integration/test_status_counters.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from coppermind_api.deps import store
from coppermind_api.main import create_app
from fastapi.testclient import TestClient

from coppermind.api_keys import ApiKeySet, create_key
from coppermind.settings import Wiring
from coppermind.store_protocol import (
    CreatedNote,
    CreatedSource,
    CreateNote,
    IngestRequest,
    IngestResult,
    MetadataUnavailable,
    NoteDocument,
    NoteSourceInfo,
    NotFound,
    Page,
    SourceQuery,
    SourceSummary,
    StatusCounters,
    StatusResponse,
)

NOTE_ID = "01K4Q8Z3N7V2X9M1B5C6D8E0F2"
UNKNOWN_NOTE_ID = "01K4Q8Z3N7V2X9M1B5C6D8E0F3"
CREATED = datetime(2026, 9, 8, tzinfo=UTC)
REVIEW_FOLDER = "Inbox"
# Hashing a key is deliberately slow, so each one is made once per module.
KEYS = {
    name: create_key(name, scopes, created_at=CREATED)
    for name, scopes in {
        "sources": ["sources:read"],
        "notes": ["notes:read"],
        "both": ["notes:read", "sources:read"],
        "writer": ["notes:write", "sources:write"],
        "nothing": [],
    }.items()
}


def _source_id(index: int) -> str:
    return f"01K4Q8Z2A0P1Q2R3S4T5U6V{index:03d}"


class MemoryStore:
    """The store contract over in-memory notes and sources."""

    def __init__(self) -> None:
        self.error: Exception | None = None
        self.queries: list[SourceQuery] = []
        self.notes: dict[str, dict] = {
            NOTE_ID: {"path": f"{REVIEW_FOLDER}/Kickoff.md", "reviewed": False, "state": "ok"}
        }
        self.sources: list[SourceSummary] = []
        self.cited: dict[str, list[str]] = {NOTE_ID: []}
        self.rejected_ingests = 0
        for index, provider in enumerate(["plaud", "plaud", "granola"]):
            self._add_source(provider, f"recording-{index}", NOTE_ID)
        self.keys = {name: key for name, (_, key) in KEYS.items()}
        self.records = [record for record, _ in KEYS.values()]

    def _add_source(self, provider: str, external: str, note_id: str) -> SourceSummary:
        index = len(self.sources)
        source = SourceSummary(
            id=_source_id(index),
            provider=provider,
            external_source_id=external,
            source_type="transcript",
            origin="",
            current_revision=1,
            created_at=CREATED + timedelta(days=index),
        )
        self.sources.append(source)
        self.cited.setdefault(note_id, []).append(source.id)
        return source

    async def get_api_keys(self) -> ApiKeySet:
        return ApiKeySet(keys=self.records)

    async def is_ready(self) -> bool:
        return True

    async def list_sources(self, query: SourceQuery) -> Page[SourceSummary]:
        if self.error:
            raise self.error
        self.queries.append(query)
        matched = sorted(self.sources, key=lambda item: item.created_at, reverse=True)
        if query.provider:
            matched = [item for item in matched if item.provider == query.provider]
        if query.from_date:
            matched = [item for item in matched if item.created_at.date() >= query.from_date]
        if query.to_date:
            matched = [item for item in matched if item.created_at.date() <= query.to_date]
        if query.cursor:
            ids = [item.id for item in matched]
            matched = matched[ids.index(query.cursor) + 1 :]
        page = matched[: query.limit]
        more = len(matched) > query.limit
        return Page[SourceSummary](items=page, next_cursor=page[-1].id if more else None)

    async def get_note_sources(self, note_id: str) -> list[NoteSourceInfo]:
        if self.error:
            raise self.error
        if note_id not in self.notes:
            raise NotFound(note_id)
        return [
            NoteSourceInfo(id=source_id, projection_path=f"_Sources/{source_id}.md")
            for source_id in self.cited[note_id]
        ]

    async def get_status(self) -> StatusResponse:
        if self.error:
            raise self.error
        by_state: dict[str, int] = {}
        for note in self.notes.values():
            by_state[note["state"]] = by_state.get(note["state"], 0) + 1
        return StatusResponse(
            counters=StatusCounters(
                notes_awaiting_review=sum(
                    1
                    for note in self.notes.values()
                    if not note["reviewed"] and note["path"].startswith(f"{REVIEW_FOLDER}/")
                ),
                notes_by_state=by_state,
                sources=len(self.sources),
                rejected_ingests=self.rejected_ingests,
                name_collisions=0,
                unparseable_files=by_state.get("unparsed", 0),
            )
        )

    async def create_note(self, request: CreateNote) -> NoteDocument:
        note_id = f"01K4Q8Z3N7V2X9M1B5C6D8E{len(self.notes):03d}"
        path = f"{REVIEW_FOLDER}/{request.title}.md"
        self.notes[note_id] = {"path": path, "reviewed": False, "state": "ok"}
        self.cited[note_id] = []
        return NoteDocument(
            id=note_id,
            path=path,
            title=request.title,
            frontmatter={"id": note_id},
            body=request.body,
            content_hash="sha256:abc",
            size_bytes=1,
            updated_at=CREATED,
        )

    async def ingest(
        self, request: IngestRequest, *, payload_size_bytes: int | None = None
    ) -> IngestResult:
        note = await self.create_note(CreateNote(title=request.note.title))
        source = self._add_source(
            request.source.provider, request.source.external_source_id, note.id
        )
        return IngestResult(
            source=CreatedSource(id=source.id, revision=1, created=True),
            note=CreatedNote(id=note.id, path=note.path, created=True),
            projection_path=f"_Sources/{source.id}.md",
        )


@contextmanager
def _client(tmp_path: Path) -> Iterator[tuple[TestClient, MemoryStore]]:
    token = tmp_path / "internal-token"
    token.write_text("test-token\n", encoding="utf-8")
    app = create_app(Wiring(internal_token_file=token, store_url="http://store.invalid"))
    fake = MemoryStore()
    app.dependency_overrides[store] = lambda: fake
    with TestClient(app) as client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        yield client, fake


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[TestClient, MemoryStore]]:
    with _client(tmp_path) as pair:
        yield pair


def _as(fake: MemoryStore, name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {fake.keys[name]}"}


# GET /v1/sources


def test_sources_are_listed_newest_first_and_paged_by_cursor(api):
    client, fake = api

    first = client.get("/v1/sources", params={"limit": 2}, headers=_as(fake, "sources"))
    assert first.status_code == 200
    body = first.json()
    assert [item["id"] for item in body["items"]] == [_source_id(2), _source_id(1)]
    assert body["items"][0]["provider"] == "granola"
    assert body["next_cursor"]

    second = client.get(
        "/v1/sources",
        params={"limit": 2, "cursor": body["next_cursor"]},
        headers=_as(fake, "sources"),
    )
    assert second.status_code == 200
    assert [item["id"] for item in second.json()["items"]] == [_source_id(0)]
    assert second.json()["next_cursor"] is None


def test_source_listing_passes_provider_and_dates_to_the_store(api):
    client, fake = api

    response = client.get(
        "/v1/sources",
        params={"provider": "plaud", "from": "2026-09-09", "to": "2026-09-30"},
        headers=_as(fake, "sources"),
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [_source_id(1)]
    query = fake.queries[-1]
    assert query.provider == "plaud"
    assert str(query.from_date) == "2026-09-09"
    assert str(query.to_date) == "2026-09-30"
    assert query.limit == 50


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 201}, {"provider": ""}, {"from": "not-a-date"}, {"unknown": "x"}],
)
def test_a_malformed_source_listing_is_a_validation_error(api, params):
    client, fake = api

    response = client.get("/v1/sources", params=params, headers=_as(fake, "sources"))

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert fake.queries == []


def test_source_listing_says_when_the_mirror_is_down_rather_than_answering_empty(api):
    client, fake = api
    fake.error = MetadataUnavailable("database down")

    response = client.get("/v1/sources", headers=_as(fake, "sources"))

    assert response.status_code == 503
    assert response.json()["error"] == "metadata_unavailable"


def test_source_listing_needs_the_sources_read_scope(api):
    client, fake = api
    for name in ("notes", "writer", "nothing"):
        response = client.get("/v1/sources", headers=_as(fake, name))
        assert response.status_code == 403, name
        assert response.json()["error"] == "forbidden"
    assert client.get("/v1/sources").status_code == 401
    assert fake.queries == []


# GET /v1/notes/{id}/sources


def test_a_notes_sources_come_back_with_their_projection_paths(api):
    client, fake = api

    response = client.get(f"/v1/notes/{NOTE_ID}/sources", headers=_as(fake, "both"))

    assert response.status_code == 200
    assert response.json() == [
        {"id": _source_id(index), "projection_path": f"_Sources/{_source_id(index)}.md"}
        for index in range(3)
    ]


def test_the_sources_of_an_unknown_note_are_not_found(api):
    client, fake = api

    response = client.get(f"/v1/notes/{UNKNOWN_NOTE_ID}/sources", headers=_as(fake, "both"))

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_a_notes_sources_need_both_read_scopes(api):
    client, fake = api
    for name in ("notes", "sources", "writer", "nothing"):
        response = client.get(f"/v1/notes/{NOTE_ID}/sources", headers=_as(fake, name))
        assert response.status_code == 403, name
        assert response.json()["error"] == "forbidden"
    assert client.get(f"/v1/notes/{NOTE_ID}/sources").status_code == 401


# GET /v1/status


def test_status_returns_every_counter(api):
    client, fake = api

    response = client.get("/v1/status", headers=_as(fake, "nothing"))

    assert response.status_code == 200
    assert response.json() == {
        "counters": {
            "notes_awaiting_review": 1,
            "notes_by_state": {"ok": 1},
            "sources": 3,
            "rejected_ingests": 0,
            "name_collisions": 0,
            "unparseable_files": 0,
        }
    }


def test_status_counters_follow_a_new_note_and_a_new_source(api):
    client, fake = api
    before = client.get("/v1/status", headers=_as(fake, "sources")).json()["counters"]

    created = client.post("/v1/notes", json={"title": "Follow up"}, headers=_as(fake, "writer"))
    assert created.status_code == 201
    after_note = client.get("/v1/status", headers=_as(fake, "sources")).json()["counters"]
    assert after_note["notes_awaiting_review"] == before["notes_awaiting_review"] + 1
    assert after_note["notes_by_state"]["ok"] == before["notes_by_state"]["ok"] + 1
    assert after_note["sources"] == before["sources"]

    ingested = client.post(
        "/v1/ingest",
        json={
            "source": {
                "provider": "plaud",
                "external_source_id": "recording-new",
                "source_type": "transcript",
                "artifacts": [
                    {"name": "transcript.txt", "mime_type": "text/plain", "content": "hello"}
                ],
            },
            "note": {"title": "Recorded call"},
        },
        headers=_as(fake, "writer"),
    )
    assert ingested.status_code == 201
    after_source = client.get("/v1/status", headers=_as(fake, "sources")).json()["counters"]
    assert after_source["sources"] == before["sources"] + 1
    assert after_source["notes_awaiting_review"] == after_note["notes_awaiting_review"] + 1

    listed = client.get("/v1/sources", params={"limit": 1}, headers=_as(fake, "sources"))
    assert listed.json()["items"][0]["external_source_id"] == "recording-new"


def test_status_needs_a_valid_key(api):
    client, fake = api
    for authorization in ("", "Bearer wrong", f"Bearer {fake.keys['nothing']}x"):
        response = client.get("/v1/status", headers={"Authorization": authorization})
        assert response.status_code == 401
        assert response.json()["error"] == "unauthorized"


def test_status_says_when_the_mirror_is_down(api):
    client, fake = api
    fake.error = MetadataUnavailable("database down")

    response = client.get("/v1/status", headers=_as(fake, "nothing"))

    assert response.status_code == 503
    assert response.json()["error"] == "metadata_unavailable"


@pytest.mark.parametrize("method", ["put", "patch", "delete", "post"])
def test_the_source_listing_refuses_every_mutation(api, method: str):
    client, fake = api

    response = getattr(client, method)("/v1/sources", headers=_as(fake, "sources"))

    assert response.status_code == 405
    assert response.json()["error"] == "method_not_allowed"
