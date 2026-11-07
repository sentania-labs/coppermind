"""Retention controls, exact preview, destructive confirmation and CSRF."""

import re
from datetime import UTC, datetime
from typing import Literal

from coppermind.purge import PurgeBreakdown, PurgeCandidate, PurgeCounts, PurgeKept, PurgeResult
from coppermind.settings import read_settings
from coppermind.store_protocol import (
    NotesFilesystemUnavailable,
    ProblemInfo,
    SourcesFilesystemUnavailable,
)
from services.admin.tests.browser import admin_browser as admin_browser
from services.admin.tests.test_admin import form_token


class PurgeStore:
    def __init__(self):
        self.confirmed = []
        self.abandoned = []
        self.empty = False
        self.state: Literal["preview", "failed"] = "preview"
        self.problems: list[ProblemInfo] = []
        self.previews = 0
        self.kind: Literal["note", "source"] = "note"

    async def get_problems(self):
        return list(self.problems)

    async def preview_purge(self):
        self.previews += 1
        return PurgeResult(
            token="a" * 64,
            state=self.state,
            created_at=datetime.now(UTC),
            retention_days=30,
            candidates=[]
            if self.empty
            else [
                PurgeCandidate(
                    kind=self.kind,
                    id="test-id",
                    title="<script>bad</script>",
                    path="Review/Old.md",
                    origins=["Local notes"],
                    providers=["Local notes"],
                    deleted_at=datetime(2026, 9, 1, tzinfo=UTC),
                )
            ],
            counts=PurgeCounts(
                notes=0 if self.empty or self.kind == "source" else 1,
                sources=1 if self.kind == "source" and not self.empty else 0,
                by_origin={"Local notes": PurgeBreakdown(notes=1)},
                by_provider={"Local notes": PurgeBreakdown(notes=1)},
            ),
            kept=PurgeKept(notes_within_retention=2, sources_in_use=1),
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


def test_preview_separates_notes_from_sources_and_says_what_is_kept(signed_in):
    client, _ = signed_in
    client.app.state.store = PurgeStore()
    csrf = form_token(client, "/admin/purge")
    page = client.post("/v1/admin/purge/preview", data={"csrf": csrf}).text
    assert "<th>Deleted notes</th><th>Orphaned sources</th>" in page
    assert "Kept: 2 deleted notes still inside the 30-day retention window" in page
    assert "1 source still used by live notes" in page
    assert page.count("Local notes") == 3  # one group row per table and one item row
    assert "<code>Purge listed items</code>" in page
    assert "Back to retention settings (nothing removed)" in page
    assert "0 orphaned" not in page and "Cancel and keep" not in page


def test_confirmation_wording_agrees_with_a_single_source(signed_in):
    client, _ = signed_in
    fake = PurgeStore()
    fake.kind = "source"
    client.app.state.store = fake
    csrf = form_token(client, "/admin/purge")
    page = client.post("/v1/admin/purge/preview", data={"csrf": csrf}).text
    assert "1 orphaned source with its original bundle and generated pages" in page
    assert "deleted note" not in page.split("Confirm permanent removal")[1].split("</p>")[0]


def test_purge_page_has_no_appearance_of_its_own(signed_in):
    client, _ = signed_in
    client.app.state.store = PurgeStore()
    csrf = form_token(client, "/admin/purge")

    def styles(text):
        return re.findall(r"<style.*?</style>", text, flags=re.S)

    purge_page = client.post("/v1/admin/purge/preview", data={"csrf": csrf}).text
    other = client.get("/admin/settings").text
    assert styles(purge_page) == styles(other)


def test_unfinished_purge_is_shown_when_the_owner_lands_on_the_page(signed_in):
    client, _ = signed_in
    fake = PurgeStore()
    client.app.state.store = fake
    quiet = client.get("/admin/purge").text
    assert "needs attention" not in quiet and "Review the unfinished purge" not in quiet
    fake.problems = [
        ProblemInfo(
            kind="purge_unfinished",
            reference="a" * 64,
            reason="Retention purge is unfinished. Open Retention purge to retry the saved list.",
        ),
        ProblemInfo(kind="damaged_source", reference="b" * 26, reason="Damaged bundle elsewhere."),
    ]
    page = client.get("/admin/purge").text
    assert "A purge needs attention" in page and "Retention purge is unfinished" in page
    assert "Review the unfinished purge" in page and "Damaged bundle elsewhere" not in page
    assert fake.previews == 0
    reviewed = client.post(
        "/v1/admin/purge/preview", data={"csrf": form_token(client, "/admin/purge")}
    )
    assert fake.previews == 1 and reviewed.status_code == 200


def test_unsaved_days_are_never_silently_discarded(signed_in):
    client, _ = signed_in
    fake = PurgeStore()
    client.app.state.store = fake
    csrf = form_token(client, "/admin/purge")
    refused = client.post("/v1/admin/purge/preview", data={"csrf": csrf, "days": "5"})
    assert refused.status_code == 422 and "have not saved it" in refused.text
    assert fake.previews == 0 and 'value="5"' in refused.text
    saved = str(read_settings(client.app.state.control).retention.days)
    allowed = client.post("/v1/admin/purge/preview", data={"csrf": csrf, "days": saved})
    assert allowed.status_code == 200 and fake.previews == 1


def test_a_failed_confirmation_names_the_volume_and_the_way_back(signed_in):
    client, _ = signed_in
    fake = PurgeStore()
    client.app.state.store = fake
    csrf = form_token(client, "/admin/purge")
    body = {"csrf": csrf, "token": "a" * 64, "confirmation": "Purge listed items"}

    async def sources_down(payload):
        raise SourcesFilesystemUnavailable("sources")

    fake.confirm_purge = sources_down  # type: ignore[method-assign]
    sources = client.post("/v1/admin/purge/confirm", data=body)
    assert sources.status_code == 503
    assert "The sources volume could not be read or written." in sources.text
    assert 'href="/admin/purge"' in sources.text and "retry it or abandon it" in sources.text

    async def notes_down(payload):
        raise NotesFilesystemUnavailable("notes")

    fake.confirm_purge = notes_down  # type: ignore[method-assign]
    notes = client.post("/v1/admin/purge/confirm", data=body)
    assert "The notes or state volume could not be read or written." in notes.text
    assert "the notes filesystem" not in notes.text


def test_retention_says_it_covers_removed_artifacts(signed_in):
    client, _ = signed_in
    client.app.state.store = PurgeStore()
    text = " ".join(client.get("/admin/purge").text.split())
    assert "Keep deleted notes, removed artifacts and dossiers for (days)" in text
    assert "the stored files of removed artifacts" in text
    assert "an artifact removed here, through the API or by deleting its file in Obsidian" in text
    settings = client.get("/admin/settings?section=retention").text
    assert "Keep deleted notes, removed artifacts and dossiers for (days)" in settings


def test_retention_has_one_home(signed_in):
    client, _ = signed_in
    moved = client.get("/admin/settings?section=retention", follow_redirects=False)
    assert moved.status_code == 303 and moved.headers["location"] == "/admin/purge"
    posted = client.post(
        "/v1/admin/settings?section=retention",
        data={"csrf": form_token(client, "/admin/purge")},
        follow_redirects=False,
    )
    assert posted.status_code == 303 and posted.headers["location"] == "/admin/purge"


def test_a_failed_page_load_is_not_reported_as_a_failed_save(signed_in, monkeypatch):
    from coppermind_admin.pages import purge as page

    client, _ = signed_in
    client.app.state.store = PurgeStore()

    def unreadable(control):
        raise OSError("settings volume unavailable")

    monkeypatch.setattr(page, "read_settings", unreadable)
    response = client.get("/admin/purge")
    assert response.status_code == 500
    assert "Retention settings could not be read." in response.text
    assert "could not be saved" not in response.text
    assert "interrupted purge" not in response.text


def test_interrupted_purge_warning_only_where_removal_may_have_started(signed_in):
    from coppermind.store_protocol import MetadataUnavailable, PurgeConflict, ValidationFailed

    client, _ = signed_in
    fake = PurgeStore()
    client.app.state.store = fake
    csrf = form_token(client, "/admin/purge")
    body = {"csrf": csrf, "token": "a" * 64, "confirmation": "Purge listed items"}
    warning = "An interrupted purge may have removed some items already"

    def failing(error):
        async def fail(payload=None):
            raise error

        return fail

    fake.preview_purge = failing(NotesFilesystemUnavailable("notes"))  # type: ignore[method-assign]
    preview = client.post("/v1/admin/purge/preview", data={"csrf": csrf})
    assert preview.status_code == 503 and warning not in preview.text
    assert "Reload retention purge" in preview.text

    fake.abandon_purge = failing(NotesFilesystemUnavailable("notes"))  # type: ignore[attr-defined]
    abandon = client.post("/v1/admin/purge/abandon", data={"csrf": csrf, "token": "a" * 64})
    assert abandon.status_code == 503 and warning not in abandon.text

    for refusal in (PurgeConflict(), ValidationFailed(["Run a new preview."])):
        fake.confirm_purge = failing(refusal)  # type: ignore[method-assign]
        refused = client.post("/v1/admin/purge/confirm", data=body)
        assert refused.status_code in {409, 422} and warning not in refused.text

    for error in (NotesFilesystemUnavailable("notes"), MetadataUnavailable("db")):
        fake.confirm_purge = failing(error)  # type: ignore[method-assign]
        failed = client.post("/v1/admin/purge/confirm", data=body)
        assert failed.status_code == 503 and warning in failed.text
        assert "Open retention purge" in failed.text
