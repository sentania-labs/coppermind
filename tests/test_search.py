"""Search without a database: query building, paging, filters and the public route.

The statements are compiled with the PostgreSQL dialect and read as SQL, which
is what proves the filters combine and the keyset resumes where it should
without a server. What PostgreSQL then does with them, ranking, excerpts and
the index following every write, runs against a real server in
tests/integration/test_search_index.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from coppermind_admin.pages.search_index import local_time
from coppermind_api.deps import store
from coppermind_api.main import create_app
from coppermind_store.indexer import MAX_INDEXED_CHARS, indexable
from coppermind_store.search import (
    SearchCursor,
    build_search,
    decode_cursor,
    encode_cursor,
    excluded_terms,
    filter_conditions,
    folder_of,
    page_of,
    query_digest,
    tidy_excerpt,
)
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from coppermind.api_keys import ApiKeySet, create_key
from coppermind.settings import Wiring
from coppermind.store_protocol import (
    MetadataUnavailable,
    Page,
    SearchHit,
    SearchQuery,
    ValidationFailed,
)

NOTE_ID = "01K4Q8Z3N7V2X9M1B5C6D8E0F2"
SOURCE_ID = "01K4Q8Z2A0P1Q2R3S4T5U6V7W8"
CREATED = datetime(2026, 10, 9, tzinfo=UTC)


def _query(**values: Any) -> SearchQuery:
    return SearchQuery.model_validate({"q": "budget review", **values})


def _sql(query: SearchQuery, after: SearchCursor | None = None) -> tuple[str, dict[str, Any]]:
    compiled = build_search(query, after).compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


# --- the question -----------------------------------------------------------


def test_the_question_is_asked_in_english_and_as_written():
    sql, params = _sql(_query())

    assert sql.count("websearch_to_tsquery") >= 2
    assert "CAST(%(param_1)s AS REGCONFIG)" in sql
    assert "english" in params.values()
    assert "simple" in params.values()
    assert "budget review" in params.values()
    assert "search_documents.search_vector @@" in sql
    assert "ts_rank(search_documents.search_vector" in sql
    assert "ts_headline(" in sql


def test_the_question_is_a_bound_value_never_spliced_into_sql():
    hostile = "x'); DROP TABLE notes; --"
    sql, params = _sql(_query(q=hostile))

    assert "DROP TABLE" not in sql
    assert hostile in params.values()


@pytest.mark.parametrize(
    ("q", "excluded"),
    [
        ("budget -runs", ["runs"]),
        ('budget -"weekly review" -draft', ['"weekly review"', "draft"]),
        ("e-mail budget", []),
        ("budget - runs", []),
        ("cats or -dogs", []),
        ("-dogs or cats", []),
        ("-dogs", ["dogs"]),
    ],
)
def test_the_words_a_question_excludes_are_found(q: str, excluded: list[str]):
    assert excluded_terms(q) == excluded


def test_an_exclusion_is_refused_under_both_readings():
    sql, params = _sql(_query(q="budget -runs"))

    # The whole question, then the excluded word alone in each configuration.
    assert sql.count("websearch_to_tsquery") >= 4
    assert "NOT (search_documents.search_vector @@" in sql
    assert "runs" in params.values()


@pytest.mark.parametrize("q", ["", "   ", "x" * 501])
def test_an_empty_or_oversized_question_is_refused(q: str):
    with pytest.raises(ValidationError):
        _query(q=q)


def test_without_filters_only_the_base_conditions_apply():
    assert filter_conditions(_query()) == []
    sql, _ = _sql(_query())

    assert "notes.reviewed" not in sql.split("FROM", 1)[1]
    assert "starts_with" not in sql
    # Every hit joins the mirror, and a note it no longer holds is never one.
    assert "LEFT OUTER JOIN notes" in sql
    assert "coalesce(notes.state" in sql


# --- filters -----------------------------------------------------------------


FILTERS: dict[str, tuple[Any, str]] = {
    "folder": ("Work/Clients", "starts_with(search_documents.path"),
    "reviewed": (True, "notes.reviewed IS true"),
    "type": ("meeting", "notes.type ="),
    "context": ("work", "notes.context ="),
    "account": ("Acme", "notes.account ="),
    "from": ("2026-09-01", "notes.date >="),
    "to": ("2026-09-30", "notes.date <="),
    "tag": ("budget", "= any(notes.tags)"),
    "state": ("unparsed", "coalesce(notes.state"),
}


@pytest.mark.parametrize("name", sorted(FILTERS))
def test_each_listing_filter_narrows_the_search(name: str):
    value, fragment = FILTERS[name]
    query = _query(**{name: value})
    sql, _ = _sql(query)

    assert len(filter_conditions(query)) == 1
    assert fragment in sql


def test_every_filter_combines_with_every_other():
    query = _query(**{name: value for name, (value, _) in FILTERS.items()})
    sql, params = _sql(query)

    assert len(filter_conditions(query)) == len(FILTERS)
    for _, fragment in FILTERS.values():
        assert fragment in sql
    # Conditions are joined with AND, never OR: each filter narrows further.
    where = sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    assert where.count(" AND ") >= len(FILTERS)
    assert "Work/Clients/" in params.values()
    assert date(2026, 9, 1) in params.values()
    assert "budget" in params.values()


def test_a_folder_filter_is_a_path_prefix_the_same_way_listing_reads_it():
    _, params = _sql(_query(folder="Review"))

    assert "Review/" in params.values()
    assert "Review" not in params.values()


@pytest.mark.parametrize("name", ["folder", "type", "context", "account", "tag"])
def test_an_empty_text_filter_is_refused_as_in_listing(name: str):
    with pytest.raises(ValidationError):
        _query(**{name: ""})


def test_reviewed_false_is_a_filter_not_an_absence():
    query = _query(reviewed=False)
    sql, _ = _sql(query)

    assert "notes.reviewed IS false" in sql


# --- paging ------------------------------------------------------------------


@pytest.mark.parametrize(("limit", "fetched"), [(None, 51), (1, 2), (200, 201)])
def test_a_page_asks_for_one_more_hit_than_it_returns(limit: int | None, fetched: int):
    query = _query() if limit is None else _query(limit=limit)
    _, params = _sql(query)

    assert fetched in params.values()


@pytest.mark.parametrize("limit", [0, 201])
def test_the_limit_has_listings_bounds(limit: int):
    with pytest.raises(ValidationError):
        _query(limit=limit)


def test_the_cursor_resumes_after_the_last_hit_by_rank_then_identity():
    after = SearchCursor(query_digest(_query().q), 0.25, "note", NOTE_ID)
    sql, params = _sql(_query(), after)

    assert "ts_rank(search_documents.search_vector" in sql
    assert "(search_documents.kind, search_documents.ref_id) >" in sql
    assert 0.25 in params.values()
    assert NOTE_ID in params.values()
    order = sql.rsplit("ORDER BY", 1)[1]
    assert order.strip().startswith("page.rank DESC, page.kind, page.ref_id")


def test_a_cursor_round_trips_exactly():
    rank = 0.0607927106320858
    cursor = SearchCursor(query_digest("budget review"), rank, "source", SOURCE_ID)

    decoded = decode_cursor(encode_cursor(cursor), "budget review")

    assert decoded == cursor
    assert decoded.rank == rank


def test_a_cursor_from_another_question_is_refused():
    cursor = encode_cursor(SearchCursor(query_digest("budget"), 0.5, "note", NOTE_ID))

    with pytest.raises(ValidationFailed) as refused:
        decode_cursor(cursor, "roadmap")

    assert "different q" in refused.value.errors[0]


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64 at all!",
        "eyJ2IjoyfQ",  # a listing cursor, version 2
        encode_cursor(SearchCursor(query_digest("q"), 0.1, "note", NOTE_ID)).replace("e", "f"),
    ],
)
def test_a_malformed_cursor_is_refused_as_invalid(cursor: str):
    with pytest.raises(ValidationFailed):
        decode_cursor(cursor, "q")


def _row(ref_id: str, rank: float, kind: str = "note", path: str = "Work/Plan.md") -> Any:
    return SimpleNamespace(
        kind=kind,
        ref_id=ref_id,
        path=path,
        title="Plan",
        rank=rank,
        state="ok",
        excerpt="the **budget**\n\nfor the\treview",
    )


def test_the_extra_row_becomes_the_next_cursor_and_is_not_returned():
    query = _query(limit=2)
    rows = [_row("a", 0.9), _row("b", 0.5), _row("c", 0.1)]

    page = page_of(rows, query)

    assert [hit.id for hit in page.items] == ["a", "b"]
    assert page.next_cursor is not None
    resumed = decode_cursor(page.next_cursor, query.q)
    assert (resumed.rank, resumed.kind, resumed.ref_id) == (0.5, "note", "b")


def test_the_last_page_has_no_cursor():
    page = page_of([_row("a", 0.9)], _query(limit=2))

    assert page.next_cursor is None
    assert len(page.items) == 1


def test_a_hit_says_what_it_is_and_where_it_lives():
    page = page_of(
        [
            _row(NOTE_ID, 0.9),
            _row(SOURCE_ID, 0.3, kind="source", path="_Sources/Plaud/2026-10-09 Call.md"),
            _row("root", 0.1, path="Loose.md"),
        ],
        _query(),
    )

    note, source, loose = page.items
    assert (note.kind, note.folder) == ("note", "Work")
    assert (source.kind, source.id, source.folder) == ("source", SOURCE_ID, "_Sources/Plaud")
    assert loose.folder == ""
    assert note.excerpt == "the **budget** for the review"


def test_excerpts_and_folders_are_tidied():
    assert tidy_excerpt(None) == ""
    assert tidy_excerpt("a\n\nb  c") == "a b c"
    assert folder_of("a/b/c.md") == "a/b"


def test_indexed_text_drops_nul_and_is_bounded():
    assert indexable("a\x00b") == "ab"
    assert len(indexable("x" * (MAX_INDEXED_CHARS + 10))) == MAX_INDEXED_CHARS


def test_admin_shows_index_times_in_local_time():
    moment = datetime(2026, 10, 9, 19, 5, tzinfo=UTC)

    assert local_time(moment, ZoneInfo("America/Chicago")) == "October 9, 2026, 2:05 PM CDT"
    assert local_time(None, ZoneInfo("America/Chicago")) == "never"


# --- the public route ----------------------------------------------------------


KEYS = {
    name: create_key(name, scopes, created_at=CREATED)
    for name, scopes in {
        "reader": ["notes:read"],
        "sources": ["sources:read"],
        "writer": ["notes:write"],
    }.items()
}


class SearchingStore:
    """The store contract's search, answering from a fixed list of hits."""

    def __init__(self) -> None:
        self.queries: list[SearchQuery] = []
        self.error: Exception | None = None
        self.records = [record for record, _ in KEYS.values()]
        self.keys = {name: key for name, (_, key) in KEYS.items()}

    async def get_api_keys(self) -> ApiKeySet:
        return ApiKeySet(keys=self.records)

    async def is_ready(self) -> bool:
        return True

    async def search(self, query: SearchQuery) -> Page[SearchHit]:
        if self.error:
            raise self.error
        self.queries.append(query)
        hit = SearchHit(
            id=SOURCE_ID,
            kind="source",
            title="Call (source)",
            path="_Sources/Plaud/2026-10-09 Call.md",
            folder="_Sources/Plaud",
            excerpt="the **budget**",
            rank=0.5,
            state="ok",
        )
        return Page[SearchHit](items=[hit], next_cursor="next")


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[TestClient, SearchingStore]]:
    token = tmp_path / "internal-token"
    token.write_text("test-token\n", encoding="utf-8")
    app = create_app(Wiring(internal_token_file=token, store_url="http://store.invalid"))
    fake = SearchingStore()
    app.dependency_overrides[store] = lambda: fake
    with TestClient(app) as client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        yield client, fake


def _as(fake: SearchingStore, name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {fake.keys[name]}"}


def test_search_needs_a_key_with_notes_read(api):
    client, fake = api

    assert client.get("/v1/search", params={"q": "budget"}).status_code == 401
    for name in ("sources", "writer"):
        response = client.get("/v1/search", params={"q": "budget"}, headers=_as(fake, name))
        assert response.status_code == 403, name
    assert fake.queries == []


def test_search_passes_every_filter_and_the_page_through(api):
    client, fake = api
    params = {
        "q": "budget",
        "folder": "Work",
        "reviewed": "false",
        "type": "meeting",
        "context": "work",
        "account": "Acme",
        "from": "2026-09-01",
        "to": "2026-09-30",
        "tag": "budget",
        "state": "ok",
        "limit": "10",
        "cursor": "abc",
    }

    response = client.get("/v1/search", params=params, headers=_as(fake, "reader"))

    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] == "next"
    assert body["items"][0]["kind"] == "source"
    assert set(body["items"][0]) == {
        "id",
        "kind",
        "title",
        "path",
        "folder",
        "excerpt",
        "rank",
        "state",
    }
    (query,) = fake.queries
    assert query.q == "budget"
    assert (query.folder, query.reviewed, query.type) == ("Work", False, "meeting")
    assert (query.context, query.account) == ("work", "Acme")
    assert (query.tag, query.state) == ("budget", "ok")
    assert (query.from_date, query.to_date) == (date(2026, 9, 1), date(2026, 9, 30))
    assert (query.limit, query.cursor) == (10, "abc")


@pytest.mark.parametrize(
    "params",
    [{}, {"q": ""}, {"q": "budget", "limit": "0"}, {"q": "budget", "folder": ""}],
)
def test_a_question_search_cannot_ask_is_refused_before_the_store(api, params):
    client, fake = api

    response = client.get("/v1/search", params=params, headers=_as(fake, "reader"))

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert fake.queries == []


def test_search_without_the_database_answers_503(api):
    client, fake = api
    fake.error = MetadataUnavailable("database is down")

    response = client.get("/v1/search", params={"q": "budget"}, headers=_as(fake, "reader"))

    assert response.status_code == 503
    assert response.json()["error"] == "metadata_unavailable"


def test_search_is_in_the_published_contract(api):
    client, _ = api

    schema = client.get("/openapi.json").json()

    assert "/v1/search" in schema["paths"]
    parameters = {item["name"] for item in schema["paths"]["/v1/search"]["get"]["parameters"]}
    assert {"q", "folder", "reviewed", "tag", "state", "from", "to", "cursor", "limit"} <= (
        parameters
    )
