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
    ReconcilerNotice,
    SourceManifest,
    SourceRevision,
    StatusCounters,
    StatusResponse,
    StoreUnavailable,
)
from services.admin.tests.browser import admin_browser as admin_browser
from services.admin.tests.test_admin import form_token

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
                known_unreadable=sum(p.kind == "search_unreadable" for p in self.problems),
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


def test_exclusions_do_not_inflate_problem_counter_and_are_separated(signed_in):
    from coppermind_admin.pages.problems import counters_html

    assert "Problems (0)" in counters_html(
        StatusResponse(
            counters=StatusCounters(
                notes_awaiting_review=0,
                notes_by_state={},
                sources=0,
                rejected_ingests=0,
                name_collisions=0,
                unparseable_files=0,
                excluded_count=8,
            )
        )
    )
    client, _ = signed_in
    store = ReadOnlyStore()
    store.problems.append(
        ProblemInfo(kind="excluded", reference="Templates/example.md", reason="excluded folder")
    )
    client.app.state.store = store
    response = client.get("/admin/problems")
    assert response.status_code == 200
    failures, exclusions = response.text.split("<h2>Intentional exclusions</h2>")
    assert "Templates/example.md" not in failures
    assert "Templates/example.md" in exclusions


def test_known_unreadable_search_entries_are_counted_and_linked(signed_in):
    client, _ = signed_in
    store = _install(client, ReadOnlyStore())
    store.problems.append(
        ProblemInfo(
            kind="search_unreadable",
            reference="Sources/missing.md",
            reason="FileNotFoundError: missing projection",
            source_id=SOURCE_ID,
        )
    )
    response = client.get("/admin/problems")
    assert response.status_code == 200
    assert "Known unreadable search entries: 1" in response.text
    assert "Sources/missing.md" in response.text
    assert "FileNotFoundError: missing projection" in response.text
    assert f'href="/admin/sources/{SOURCE_ID}"' in response.text

    status = client.get("/v1/admin/status")
    assert status.status_code == 200
    assert status.json()["counters"]["known_unreadable"] == 1
    overview = client.get("/admin")
    assert "Known unreadable search entries: 1" in overview.text


def test_calendar_and_dropbox_failures_count_and_link_to_dismissal(signed_in):
    class CalendarProblems(ReadOnlyStore):
        async def get_status(self):
            result = await super().get_status()
            result.counters.active_problems = 2
            return result

    client, _ = signed_in
    fake = CalendarProblems()
    fake.problems = [
        ProblemInfo(kind="calendar_import", reference="conflict.zip", reason="Planning conflicts"),
        ProblemInfo(kind="dropbox", reference="handout.pdf", reason="Unsupported file type"),
    ]
    _install(client, fake)
    assert "Problems (2)" in client.get("/admin").text
    page = client.get("/admin/problems")
    assert 'href="/admin/context?tab=calendar"' in page.text
    assert 'href="/admin/workflow?tab=intake"' in page.text
    assert "<td>Calendar import</td>" in page.text
    assert "<td>Dropbox</td>" in page.text
    assert "No excluded files." in page.text
    assert "No problems found." not in page.text


def test_an_unreadable_purge_receipt_offers_abandon(signed_in):
    client, _ = signed_in
    store = ReadOnlyStore()
    store.problems = [
        ProblemInfo(kind="purge_receipt", reference="a" * 64, reason="A receipt is unreadable.")
    ]
    _install(client, store)

    response = client.get("/admin/problems")

    assert response.status_code == 200
    assert 'action="/v1/admin/purge/abandon"' in response.text
    assert f'name="token" value="{"a" * 64}"' in response.text
    assert 'name="csrf"' in response.text


def test_the_overview_count_includes_purge_problems():
    from coppermind_admin.pages.problems import counters_html

    html = counters_html(
        StatusResponse(
            counters=StatusCounters(
                notes_awaiting_review=0,
                notes_by_state={},
                sources=0,
                rejected_ingests=1,
                name_collisions=0,
                unparseable_files=0,
                purge_problems=3,
            )
        )
    )
    assert "Problems (4)" in html and "Retention purge items needing attention: 3" in html


def test_a_damaged_source_bundle_is_plain_and_has_no_dead_link(signed_in):
    client, _ = signed_in
    store = _install(client, ReadOnlyStore())
    store.problems = [
        ProblemInfo(
            kind="damaged_source",
            reference=SOURCE_ID,
            reason="The preserved copy of plaud source recording-1 has files but no manifest.",
            source_id=SOURCE_ID,
        )
    ]
    page = client.get("/admin/problems").text
    assert "Damaged source bundle" in page and "Preserved source copy" in page
    assert "damaged_source" not in page and SOURCE_ID not in page
    assert "/admin/sources/" not in page


def test_counters_show_the_notes_root_and_the_pending_move_count():
    from coppermind_admin.pages.problems import counters_html

    html = counters_html(
        StatusResponse(
            counters=StatusCounters(
                notes_awaiting_review=0,
                notes_by_state={},
                sources=0,
                rejected_ingests=0,
                name_collisions=0,
                unparseable_files=0,
                notes_folder="Notes",
                notes_to_refile=7,
                notes_returned=2,
            )
        )
    )
    assert "Notes root: Notes/" in html
    assert "next filing pass will move: 7" in html
    assert "returned to their filed folder: 2" in html


class NoticeStore(ReadOnlyStore):
    """A store whose status reports returned notes and lists their notices."""

    def __init__(self) -> None:
        super().__init__()
        self.notices = [
            ReconcilerNotice(
                note_id=NOTE_ID,
                title="Warehouse <call>",
                from_path="Elsewhere/call.md",
                path="Notes/2026/2026-04/call.md",
                reason="Moved back to Notes/2026/2026-04/ because notes are filed by date.",
                at=datetime(2026, 11, 5, 15, 30, tzinfo=UTC),
            )
        ]

    async def get_status(self) -> StatusResponse:
        status = await super().get_status()
        status.counters.notes_returned = len(self.notices)
        return status

    async def list_reconciler_notices(self) -> list[ReconcilerNotice]:
        self._check("list_reconciler_notices")
        return list(self.notices)

    async def dismiss_reconciler_notice(self, note_id: str) -> None:
        self._check("dismiss_reconciler_notice")
        self.notices = [notice for notice in self.notices if notice.note_id != note_id]


def test_the_overview_lists_each_returned_note_with_where_when_and_why(signed_in):
    client, _ = signed_in
    _install(client, NoticeStore())

    text = client.get("/admin").text

    assert "returned to their filed folder: 1" in text
    assert "<strong>Warehouse &lt;call&gt;</strong>" in text
    assert "<code>Elsewhere/call.md</code>" in text
    assert "<code>Notes/2026/2026-04/call.md</code>" in text
    assert "November 5, 2026, 9:30 AM CST" in text
    assert "because notes are filed by date." in text
    assert f'action="/v1/admin/notices/{NOTE_ID}/dismiss"' in text


def test_the_overview_asks_for_no_notices_when_none_were_returned(signed_in):
    client, _ = signed_in
    store = _install(client, ReadOnlyStore())

    assert client.get("/admin").status_code == 200
    assert "list_reconciler_notices" not in store.calls


def test_dismissing_a_notice_removes_it_from_the_overview(signed_in):
    client, _ = signed_in
    store = _install(client, NoticeStore())
    token = form_token(client, "/admin")

    response = client.post(
        f"/v1/admin/notices/{NOTE_ID}/dismiss", data={"csrf": token}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    assert store.notices == []
    assert "returned to their filed folder: 0" in client.get("/admin").text


def test_dismissing_needs_the_form_token(signed_in):
    client, _ = signed_in
    store = _install(client, NoticeStore())

    assert client.post(f"/v1/admin/notices/{NOTE_ID}/dismiss", data={}).status_code == 403
    assert len(store.notices) == 1


def test_the_overview_lists_old_folders_kept_with_why():
    from coppermind_admin.pages.problems import counters_html

    html = counters_html(
        StatusResponse(
            counters=StatusCounters(
                notes_awaiting_review=0,
                notes_by_state={},
                sources=0,
                rejected_ingests=0,
                name_collisions=0,
                unparseable_files=0,
                folders_kept=1,
            )
        ),
        [
            ReconcilerNotice(
                note_id="folder:2026-08",
                title="2026-08/ was kept",
                from_path="2026-08",
                path="2026-08",
                reason="2026-08/ still holds files.",
                at=datetime(2026, 11, 5, 15, 30, tzinfo=UTC),
                kind="folder",
            )
        ],
    )

    assert "left in place because they are not empty: 1" in html
    assert "<strong>2026-08/ was kept</strong>" in html
    assert "<code>2026-08</code>" in html
    assert "2026-08/ still holds files." in html
    assert 'action="/v1/admin/notices/folder%3A2026-08/dismiss"' in html
