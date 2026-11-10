"""The Admin artifacts page: the list says the kind, and remove asks first.

Admin reaches the store over the internal contract, so a stand-in answers the
three calls this page makes and fails the test on any other.
"""

from __future__ import annotations

from datetime import UTC, datetime

from coppermind.store_protocol import (
    ArtifactMissing,
    ArtifactQuery,
    ArtifactRecord,
    ArtifactRevision,
    ArtifactSummary,
    CitedNote,
    Page,
    ReconcilerNotice,
    StoreUnavailable,
)
from services.admin.tests.browser import admin_browser as admin_browser
from services.admin.tests.test_admin import form_token

ARTIFACT_ID = "01K4Q8Z2A0P1Q2R3S4T5U6V7W8"
NOTE_ID = "01K4Q8Z3N7V2X9M1B5C6D8E0F2"
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)


def record(**overrides) -> ArtifactRecord:
    values = dict(
        id=ARTIFACT_ID,
        title="VCDX <design> package",
        origin="dropbox",
        received_at=NOW,
        filename="VCDX design package.pptx",
        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        sha256="abc",
        size_bytes=3 * 1024 * 1024,
        tags=["reference"],
        path="Artifacts/2026/2026-11/VCDX design package.pptx",
        revision=2,
        citing_note_ids=[NOTE_ID],
        citing_notes=[
            CitedNote(id=NOTE_ID, title="Platform review <notes>", path="Notes/2026/2026-11/p.md")
        ],
        text_status="extracted",
        text="Slide 1",
        extractor_version="1",
        revisions=[
            ArtifactRevision(
                revision=1, stored_at=NOW, filename="a.pptx", sha256="a", size_bytes=1
            ),
            ArtifactRevision(
                revision=2, stored_at=NOW, filename="a.pptx", sha256="abc", size_bytes=3
            ),
        ],
        created_at=NOW,
        updated_at=NOW,
    )
    values.update(overrides)
    return ArtifactRecord.model_validate(values)


class ArtifactStore:
    def __init__(self, *, reachable: bool = True) -> None:
        self.reachable = reachable
        self.calls: list[str] = []
        self.removed: list[str] = []
        self.dismissed: list[str] = []
        self.notices: list[ReconcilerNotice] = []
        self.records = [
            record(),
            record(
                id="01K4Q8Z2A0P1Q2R3S4T5U6V7W9",
                title="A paper",
                origin="api",
                filename=None,
                mime_type=None,
                sha256=None,
                size_bytes=0,
                path=None,
                url="https://example.test/paper",
                citing_note_ids=[],
                citing_notes=[],
                text_status="link",
                state="ok",
            ),
        ]

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if not self.reachable:
            raise StoreUnavailable("the store is down")

    async def list_artifacts(self, query: ArtifactQuery) -> Page[ArtifactSummary]:
        self._check("list_artifacts")
        items = [
            ArtifactSummary.model_validate(
                item.model_dump(exclude={"text", "revisions", "extractor_version"})
            )
            for item in self.records
        ]
        return Page[ArtifactSummary](items=items, next_cursor=None)

    async def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        self._check("get_artifact")
        for item in self.records:
            if item.id == artifact_id:
                return item
        raise ArtifactMissing(artifact_id)

    async def remove_artifact(self, artifact_id: str, removal: str = "admin") -> None:
        self._check("remove_artifact")
        assert removal == "admin"
        if artifact_id not in {item.id for item in self.records}:
            raise ArtifactMissing(artifact_id)
        self.removed.append(artifact_id)
        self.records = [item for item in self.records if item.id != artifact_id]

    async def list_reconciler_notices(self) -> list[ReconcilerNotice]:
        self._check("list_reconciler_notices")
        return list(self.notices)

    async def dismiss_reconciler_notice(self, note_id: str) -> None:
        self._check("dismiss_reconciler_notice")
        self.dismissed.append(note_id)
        self.notices = [n for n in self.notices if n.note_id != note_id]

    async def get_status(self) -> None:
        raise StoreUnavailable("not this page's concern")

    def __getattr__(self, name: str):
        raise AssertionError(f"Admin asked the store for {name}, which this page never needs")


def test_the_navigation_links_to_artifacts(signed_in):
    client, _ = signed_in
    assert 'href="/admin/artifacts"' in client.get("/admin/settings").text


def test_the_list_names_citing_notes_by_title_and_keeps_dates_and_buttons_on_one_line(signed_in):
    client, _ = signed_in
    store = ArtifactStore()
    client.app.state.store = store
    response = client.get("/admin/artifacts")
    assert response.status_code == 200
    text = response.text
    assert "VCDX &lt;design&gt; package" in text
    assert "from dropbox" in text and "3.0 MiB" in text
    # The path wraps only after a slash; the date, size and Remove button never wrap.
    assert "Artifacts/<wbr>2026/<wbr>2026-11/<wbr>VCDX design package.pptx" in text
    assert '<td class="nowrap">Nov 4, 2026</td>' in text
    assert (
        f'<td class="nowrap"><a class="button" href="/admin/artifacts/{ARTIFACT_ID}/remove"' in text
    )
    assert f'<a href="/admin/notes/{NOTE_ID}">Platform review &lt;notes&gt;</a>' in text
    assert NOTE_ID not in text.replace(f"/admin/notes/{NOTE_ID}", "")
    assert "https://example.test/paper" in text and "no note yet" in text
    assert ARTIFACT_ID not in text.replace(f"/admin/artifacts/{ARTIFACT_ID}/remove", "")
    assert "<td>extracted<br>" in text and "revision 2" in text
    # No column repeats what every row shares: no Kind column, no Origin column saying `api`.
    assert "<th>Kind</th>" not in text and "<th>Origin</th>" not in text
    assert "<strong>artifact</strong>" not in text and "<td>api</td>" not in text
    # The intro speaks the owner's language, not the implementation's.
    for word in ("database", "state volume", "frontmatter", "byte for byte"):
        assert word not in text
    assert "shown in Obsidian under the artifacts folder" in text
    assert "Removing a file takes it out of Obsidian and search" in text
    assert "removing a link only takes it out of this list and search" in text
    assert "Removing one takes it out of Obsidian" not in text
    assert store.calls == ["list_artifacts", "list_reconciler_notices"]


def test_a_moved_artifact_returned_to_its_folder_is_shown_with_a_dismiss_that_comes_back_here(
    signed_in,
):
    client, _ = signed_in
    store = ArtifactStore()
    store.notices = [
        ReconcilerNotice(
            note_id=ARTIFACT_ID,
            title="VCDX <design> package",
            from_path="Projects/VCDX/VCDX design package.pptx",
            path="Artifacts/2026/2026-11/VCDX design package.pptx",
            reason="Moved back to Artifacts/2026/2026-11/ because artifacts are filed by the "
            "month they arrived; a move or rename in Obsidian is not kept. Nothing else changed "
            "and nothing needs doing.",
            at=NOW,
            kind="artifact",
        ),
        ReconcilerNotice(
            note_id=NOTE_ID,
            title="A note",
            from_path="Elsewhere/A note.md",
            path="Notes/2026/2026-11/A note.md",
            reason="Notes file by date.",
            at=NOW,
            kind="note",
        ),
    ]
    client.app.state.store = store
    text = client.get("/admin/artifacts").text
    assert "Moved back (1)" in text
    assert "found at <code>Projects/VCDX/VCDX design package.pptx</code>" in text
    assert "returned to <code>Artifacts/2026/2026-11/VCDX design package.pptx</code>" in text
    assert "a move or rename in Obsidian is not kept" in text
    assert "nothing needs doing" in text
    assert "Elsewhere/A note.md" not in text, "a note's notice belongs to the overview"
    assert f'action="/v1/admin/notices/{ARTIFACT_ID}/dismiss"' in text
    assert 'name="back" value="/admin/artifacts"' in text

    dismissed = client.post(
        f"/v1/admin/notices/{ARTIFACT_ID}/dismiss",
        data={"back": "/admin/artifacts", "csrf": form_token(client, "/admin/artifacts")},
        follow_redirects=False,
    )
    assert dismissed.status_code == 303
    assert dismissed.headers["location"] == "/admin/artifacts"
    assert store.dismissed == [ARTIFACT_ID]
    assert "Moved back" not in client.get("/admin/artifacts").text

    elsewhere = client.post(
        f"/v1/admin/notices/{NOTE_ID}/dismiss",
        data={"back": "https://evil.test/", "csrf": form_token(client, "/admin/artifacts")},
        follow_redirects=False,
    )
    assert elsewhere.headers["location"] == "/admin"


def test_removing_a_link_says_what_happens_to_a_link(signed_in):
    client, _ = signed_in
    store = ArtifactStore()
    client.app.state.store = store
    link_id = "01K4Q8Z2A0P1Q2R3S4T5U6V7W9"
    confirm = client.get(f"/admin/artifacts/{link_id}/remove")
    assert confirm.status_code == 200
    assert "This is a link, not a file, so nothing in Obsidian changes" in confirm.text
    assert "keep their citation and the address they already have" in confirm.text
    assert "no longer be listed or found in search" in confirm.text
    assert "<button>Remove link</button>" in confirm.text
    for wrong in ("leaves Obsidian", "Revisions kept", "deletes the copy"):
        assert wrong not in confirm.text
    removed = client.post(
        f"/v1/admin/artifacts/{link_id}/remove",
        data={"confirm": "yes", "csrf": form_token(client, f"/admin/artifacts/{link_id}/remove")},
        follow_redirects=False,
    )
    assert removed.headers["location"] == "/admin/artifacts?notice=removed_link"
    landed = client.get(removed.headers["location"]).text
    assert "The link was removed" in landed and "notes that mention it keep their address" in landed


def test_remove_asks_for_confirmation_then_removes_through_the_store(signed_in):
    client, _ = signed_in
    store = ArtifactStore()
    client.app.state.store = store
    confirm = client.get(f"/admin/artifacts/{ARTIFACT_ID}/remove")
    assert confirm.status_code == 200
    assert "Revisions kept in the archive: 2" in confirm.text
    assert "deletes the copy under the artifacts folder in Obsidian" in confirm.text
    assert "The stored file itself is kept" in confirm.text
    assert "Cited by: Platform review &lt;notes&gt;" in confirm.text
    assert f"Identifier: <code>{ARTIFACT_ID}</code>" in confirm.text
    assert store.removed == []

    unconfirmed = client.post(
        f"/v1/admin/artifacts/{ARTIFACT_ID}/remove",
        data={"csrf": form_token(client, f"/admin/artifacts/{ARTIFACT_ID}/remove")},
        follow_redirects=False,
    )
    assert unconfirmed.status_code == 303
    assert store.removed == []

    removed = client.post(
        f"/v1/admin/artifacts/{ARTIFACT_ID}/remove",
        data={
            "confirm": "yes",
            "csrf": form_token(client, f"/admin/artifacts/{ARTIFACT_ID}/remove"),
        },
        follow_redirects=False,
    )
    assert removed.status_code == 303
    assert removed.headers["location"] == "/admin/artifacts?notice=removed"
    assert store.removed == [ARTIFACT_ID]
    landed = client.get(removed.headers["location"])
    assert "The artifact was removed" in landed.text
    assert ARTIFACT_ID not in landed.text.replace("notice=removed", "")


def test_a_missing_artifact_redirects_with_a_notice(signed_in):
    client, _ = signed_in
    client.app.state.store = ArtifactStore()
    response = client.get("/admin/artifacts/nope/remove", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/artifacts?notice=missing"


def test_an_unreachable_store_is_reported_not_hidden(signed_in):
    client, _ = signed_in
    client.app.state.store = ArtifactStore(reachable=False)
    response = client.get("/admin/artifacts")
    assert response.status_code == 503
    assert "The store could not answer" in response.text


def test_the_page_needs_a_session(tmp_path):
    from services.admin.tests.test_admin import _client

    client, _, _ = _client(tmp_path)
    with client:
        response = client.get("/admin/artifacts", follow_redirects=False)
        assert response.status_code == 303
