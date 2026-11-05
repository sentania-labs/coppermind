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
    Page,
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

    async def get_status(self) -> None:
        raise StoreUnavailable("not this page's concern")

    def __getattr__(self, name: str):
        raise AssertionError(f"Admin asked the store for {name}, which this page never needs")


def test_the_navigation_links_to_artifacts(signed_in):
    client, _ = signed_in
    assert 'href="/admin/artifacts"' in client.get("/admin").text


def test_the_list_shows_kind_origin_size_where_and_citing_notes(signed_in):
    client, _ = signed_in
    store = ArtifactStore()
    client.app.state.store = store
    response = client.get("/admin/artifacts")
    assert response.status_code == 200
    text = response.text
    assert "<strong>artifact</strong>" in text
    assert "VCDX &lt;design&gt; package" in text
    assert "dropbox" in text and "3.0 MiB" in text
    assert "Artifacts/2026/2026-11/VCDX design package.pptx" in text
    assert f'href="/admin/notes/{NOTE_ID}"' in text
    assert "https://example.test/paper" in text and "uncited" in text
    assert "text extracted" in text and "revision 2" in text
    assert f'href="/admin/artifacts/{ARTIFACT_ID}/remove"' in text
    assert store.calls == ["list_artifacts"]


def test_remove_asks_for_confirmation_then_removes_through_the_store(signed_in):
    client, _ = signed_in
    store = ArtifactStore()
    client.app.state.store = store
    confirm = client.get(f"/admin/artifacts/{ARTIFACT_ID}/remove")
    assert confirm.status_code == 200
    assert "Revisions kept in the archive: 2" in confirm.text
    assert "archived bytes stay" in confirm.text
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
