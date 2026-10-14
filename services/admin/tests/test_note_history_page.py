"""Recovery preview is escaped, authenticated and CSRF guarded."""

from datetime import UTC, datetime

from coppermind.store_protocol import HistoryStatus, NoteRecovery, Page, PathCollision
from services.admin.tests.browser import admin_browser as admin_browser
from services.admin.tests.test_admin import form_token

NOTE_ID = "01K4Q8Z3N7V2X9M1B5C6D8E0F2"


class RecoveryStore:
    def __init__(self):
        self.restored = []
        self.collision = False

    async def get_note_history_status(self):
        return HistoryStatus(available=False, reason="Git unavailable", helper_status="stale")

    async def list_notes(self, query):
        return Page(items=[])

    async def preview_note_restore(self, note_id):
        return NoteRecovery(
            note_id=note_id,
            path="Review/note.md",
            content="<script>bad()</script>",
            content_hash="sha256:abc",
            removal_commit="a" * 40,
            token="b" * 64,
            recorded_at=datetime(2026, 10, 14, 19, 0, tzinfo=UTC),
        )

    async def restore_note(self, note_id, request):
        if self.collision:
            raise PathCollision("Review/note.md")
        self.restored.append((note_id, request))
        return await self.preview_note_restore(note_id)


def test_preview_warning_escaping_and_confirmation(signed_in):
    client, _ = signed_in
    store = RecoveryStore()
    client.app.state.store = store
    url = f"/admin/note-history/{NOTE_ID}"
    page = client.get(url)
    assert page.status_code == 200
    assert "&lt;script&gt;" in page.text and "<script>bad()" not in page.text
    assert "Obsidian Sync again" in page.text
    assert "2:00 PM CDT" in page.text
    assert "Their evidence" not in page.text
    token = form_token(client, url)
    result = client.post(
        f"/v1/admin/note-history/{NOTE_ID}/restore",
        data={"csrf": token, "token": "b" * 64, "confirmation": f"Restore {NOTE_ID}"},
        follow_redirects=False,
    )
    assert result.status_code == 303
    assert store.restored[0][1].confirmation == f"Restore {NOTE_ID}"


def test_csrf_and_collision_refuse_restore(signed_in):
    client, _ = signed_in
    store = RecoveryStore()
    client.app.state.store = store
    url = f"/v1/admin/note-history/{NOTE_ID}/restore"
    assert client.post(url, data={"token": "b" * 64}).status_code == 403
    assert not store.restored
    store.collision = True
    csrf = form_token(client, f"/admin/note-history/{NOTE_ID}")
    response = client.post(
        url, data={"csrf": csrf, "token": "b" * 64, "confirmation": f"Restore {NOTE_ID}"}
    )
    assert response.status_code == 409
    assert "path already in use" in response.text
    assert not store.restored


def test_git_failure_is_visible(signed_in):
    client, _ = signed_in
    client.app.state.store = RecoveryStore()
    text = client.get("/admin/note-history?state=missing").text
    assert "Git unavailable" in text
    assert "Git helper: stale" in text
