"""Retention controls, exact preview, destructive confirmation and CSRF."""

from datetime import UTC, datetime
from typing import Literal

from coppermind.purge import PurgeCandidate, PurgeCounts, PurgeResult
from coppermind.settings import read_settings
from services.admin.tests.browser import admin_browser as admin_browser
from services.admin.tests.test_admin import form_token


class PurgeStore:
    def __init__(self):
        self.confirmed = []
        self.abandoned = []
        self.empty = False
        self.state: Literal["preview", "failed"] = "preview"

    async def preview_purge(self):
        return PurgeResult(
            token="a" * 64,
            state=self.state,
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


def test_unfinished_purge_offers_abandon_and_reports_outcome(signed_in):
    client, _ = signed_in
    fake = PurgeStore()
    fake.state = "failed"
    client.app.state.store = fake
    csrf = form_token(client, "/admin/purge")
    page = client.post("/v1/admin/purge/preview", data={"csrf": csrf})
    assert "Retry removal of listed items" in page.text
    assert "/v1/admin/purge/abandon" in page.text
    assert client.post("/v1/admin/purge/abandon", data={"token": "a" * 64}).status_code == 403
    assert not fake.abandoned
    assert (
        client.post("/v1/admin/purge/abandon", data={"csrf": csrf, "token": "bad"}).status_code
        == 422
    )

    async def abandon(payload):
        fake.abandoned.append(payload)
        return (await fake.preview_purge()).model_copy(
            update={"state": "abandoned", "remaining": ["test-id"], "completed": 2}
        )

    fake.abandon_purge = abandon  # type: ignore[attr-defined]
    result = client.post("/v1/admin/purge/abandon", data={"csrf": csrf, "token": "a" * 64})
    assert result.status_code == 200
    assert "Purge abandoned" in result.text and "2 listed items were removed" in result.text
    assert "&lt;script&gt;" in result.text and "Permanently remove" not in result.text
    assert len(fake.abandoned) == 1
