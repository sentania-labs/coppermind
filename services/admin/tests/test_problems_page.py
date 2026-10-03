"""The overview counters and the problems dashboard.

Admin reaches the store over the internal contract, so these tests put a
stand-in for that contract on the app. It answers the read calls and fails the
test on any other call, which is how "the dashboard never writes into a note"
is held: there is no call Admin could make here that would change one.
"""

from __future__ import annotations

from datetime import UTC, datetime

from coppermind.store_protocol import (
    NoteSourceInfo,
    NotFound,
    ProblemInfo,
    SourceManifest,
    SourceRevision,
    StatusCounters,
    StatusResponse,
    StoreUnavailable,
)
from services.admin.tests.browser import admin_browser as admin_browser

NOTE_ID = "01K4Q8Z3N7V2X9M1B5C6D8E0F2"
SOURCE_ID = "01K4Q8Z2A0P1Q2R3S4T5U6V7W8"


class ReadOnlyStore:
    """Answers what the dashboard reads; any other call fails the test."""

    def __init__(self, *, reachable: bool = True) -> None:
        self.reachable = reachable
        self.calls: list[str] = []
        self.problems = [
            ProblemInfo(
                kind="unparsed",
                reference="Inbox/broken <b>.md",
                reason="frontmatter is not valid YAML",
                note_id=NOTE_ID,
            ),
            ProblemInfo(
                kind="ingest",
                reference="plaud:recording-1",
                reason="payload_too_large",
                source_id=SOURCE_ID,
            ),
        ]

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if not self.reachable:
            raise StoreUnavailable("the store is down")

    async def get_status(self) -> StatusResponse:
        self._check("get_status")
        return StatusResponse(
            counters=StatusCounters(
                notes_awaiting_review=5,
                notes_by_state={"ok": 7, "unparsed": 1},
                sources=10,
                rejected_ingests=2,
                name_collisions=3,
                unparseable_files=1,
            )
        )

    async def get_problems(self) -> list[ProblemInfo]:
        self._check("get_problems")
        return list(self.problems)

    async def get_note_sources(self, note_id: str) -> list[NoteSourceInfo]:
        self._check("get_note_sources")
        if note_id != NOTE_ID:
            raise NotFound(note_id)
        return [NoteSourceInfo(id=SOURCE_ID, projection_path="Sources/2026-09-08 Plaud.md")]

    async def get_source(self, source_id: str) -> SourceManifest:
        self._check("get_source")
        return SourceManifest(
            schema_version=1,
            source_id=source_id,
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
                    artifacts=[],
                )
            ],
            projection_path="Sources/2026-09-08 Plaud.md",
        )

    def __getattr__(self, name: str):
        raise AssertionError(f"Admin asked the store for {name}, which the dashboard never needs")


def _install(client, store: ReadOnlyStore) -> ReadOnlyStore:
    client.app.state.store = store
    return store


def test_the_overview_shows_the_stores_counters(signed_in):
    client, _ = signed_in
    _install(client, ReadOnlyStore())

    response = client.get("/admin")

    assert response.status_code == 200
    for line in (
        "Notes awaiting review: 5",
        "Notes by state: ok: 7, unparsed: 1",
        "Sources: 10",
        "Rejected ingests: 2",
        "Name collisions: 3",
        "Unparseable files: 1",
        '<a href="/admin/problems">Problems (6)</a>',
    ):
        assert line in response.text


def test_the_overview_still_renders_when_the_store_is_down(signed_in):
    client, _ = signed_in
    _install(client, ReadOnlyStore(reachable=False))

    response = client.get("/admin")

    assert response.status_code == 200
    assert "The store could not answer" in response.text
    assert "the store is down" not in response.text


def test_the_problems_page_lists_each_problem_with_a_link(signed_in):
    client, _ = signed_in
    store = _install(client, ReadOnlyStore())

    response = client.get("/admin/problems")

    assert response.status_code == 200
    assert "<h1>Problems</h1>" in response.text
    assert "frontmatter is not valid YAML" in response.text
    assert "Inbox/broken &lt;b&gt;.md" in response.text
    assert f'<a href="/admin/notes/{NOTE_ID}">' in response.text
    assert "payload_too_large" in response.text
    assert f'<a href="/admin/sources/{SOURCE_ID}">' in response.text
    assert store.calls == ["get_problems"]


def test_the_problem_links_open_the_note_and_the_source(signed_in):
    client, _ = signed_in
    store = _install(client, ReadOnlyStore())

    note = client.get(f"/admin/notes/{NOTE_ID}")
    source = client.get(f"/admin/sources/{SOURCE_ID}")
    unknown = client.get("/admin/notes/01K4Q8Z3N7V2X9M1B5C6D8E0F3")

    assert note.status_code == 200
    assert "frontmatter is not valid YAML" in note.text
    assert "payload_too_large" not in note.text
    assert "Sources/2026-09-08 Plaud.md" in note.text
    assert source.status_code == 200
    assert "Provider: plaud" in source.text
    assert unknown.status_code == 404
    assert set(store.calls) <= {"get_problems", "get_note_sources", "get_source"}


def test_an_empty_dashboard_says_so(signed_in):
    client, _ = signed_in
    store = _install(client, ReadOnlyStore())
    store.problems = []

    response = client.get("/admin/problems")

    assert response.status_code == 200
    assert "No problems found." in response.text


def test_the_problems_page_reports_an_unreachable_store(signed_in):
    client, _ = signed_in
    _install(client, ReadOnlyStore(reachable=False))

    response = client.get("/admin/problems")

    assert response.status_code == 503
    assert "The store could not answer" in response.text


def test_the_problem_endpoints_need_a_session(tmp_path):
    from services.admin.tests.test_admin import _client, claim

    client, _, _ = _client(tmp_path)
    with client:
        claim(client)
        _install(client, ReadOnlyStore())
        for path in ("/admin/problems", "/v1/admin/notes/problems", "/v1/admin/status"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303, path


def test_the_json_endpoints_match_the_pages(signed_in):
    client, _ = signed_in
    _install(client, ReadOnlyStore())

    problems = client.get("/v1/admin/notes/problems")
    status = client.get("/v1/admin/status")

    assert problems.status_code == 200
    assert [item["kind"] for item in problems.json()] == ["unparsed", "ingest"]
    assert status.json()["counters"]["notes_awaiting_review"] == 5
