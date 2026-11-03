"""Retention controls, exact preview, destructive confirmation and CSRF."""

from datetime import UTC, datetime

from coppermind.purge import PurgeCandidate, PurgeCounts, PurgeResult
from coppermind.settings import read_settings
from services.admin.tests.browser import admin_browser as admin_browser
from services.admin.tests.test_admin import form_token


class PurgeStore:
    def __init__(self):
        self.confirmed = []
        self.empty = False

    async def preview_purge(self):
        return PurgeResult(
            token="a" * 64,
            state="preview",
            created_at=datetime.now(UTC),
            retention_days=30,
            candidates=[]
            if self.empty
            else [
                PurgeCandidate(
                    kind="note",
                    id="test-id",
                    title="<script>bad</script>",
                    path="Review/Old.md",
                    origins=["Local notes"],
                    providers=["Local notes"],
                    deleted_at=datetime(2026, 9, 1, tzinfo=UTC),
                )
            ],
            counts=PurgeCounts(
                notes=0 if self.empty else 1,
                by_origin={"Local notes": 1},
                by_provider={"Local notes": 1},
            ),
        )

    async def confirm_purge(self, payload):
        self.confirmed.append(payload)
        return (await self.preview_purge()).model_copy(update={"state": "complete"})


def test_preview_confirm_and_csrf(signed_in):
    client, _ = signed_in
    fake = PurgeStore()
    client.app.state.store = fake
    csrf = form_token(client, "/admin/purge")
    assert client.post("/v1/admin/purge/preview").status_code == 403
    preview = client.post("/v1/admin/purge/preview", data={"csrf": csrf})
    assert preview.status_code == 200
    assert "&lt;script&gt;" in preview.text and "<script>bad" not in preview.text
    assert "test-id" not in preview.text
    assert "By origin" in preview.text and "By provider" in preview.text
    assert "Admin cannot undo this" in preview.text
    assert not fake.confirmed
    assert (
        client.post(
            "/v1/admin/purge/confirm",
            data={"token": "a" * 64, "confirmation": "Purge listed items"},
        ).status_code
        == 403
    )
    result = client.post(
        "/v1/admin/purge/confirm",
        data={"csrf": csrf, "token": "a" * 64, "confirmation": "Purge listed items"},
    )
    assert result.status_code == 200 and "Purge complete" in result.text
    assert len(fake.confirmed) == 1


def test_zero_setting_and_empty_preview(signed_in):
    client, _ = signed_in
    fake = PurgeStore()
    fake.empty = True
    client.app.state.store = fake
    csrf = form_token(client, "/admin/purge")
    revision = client.app.state.control.read("settings").revision
    assert (
        client.post(
            "/v1/admin/purge/settings", data={"csrf": csrf, "revision": revision, "days": 0}
        ).status_code
        == 200
    )
    assert read_settings(client.app.state.control).retention.days == 0
    assert (
        client.post(
            "/v1/admin/purge/settings", data={"csrf": csrf, "revision": revision, "days": 5}
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/v1/admin/purge/settings", data={"csrf": csrf, "revision": revision + 1, "days": -1}
        ).status_code
        == 422
    )
    empty = client.post("/v1/admin/purge/preview", data={"csrf": csrf})
    assert "Nothing is eligible" in empty.text
    assert "Permanently remove" not in empty.text
