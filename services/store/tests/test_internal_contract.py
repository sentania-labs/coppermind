"""The internal contract, driven end to end by the client that speaks it.

`LocalStore` and `HttpStoreClient` are interchangeable only if what the store
puts on the wire is what the client reads back off it. These tests run the real
internal router against the real client over an in process transport, so an
error shape that survives one side and not the other fails here rather than in
production.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from coppermind_store import __version__
from coppermind_store.auth import InternalAuth
from coppermind_store.control import ControlState
from coppermind_store.internal_api import router as internal_router
from coppermind_store.main import create_app
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coppermind.settings import Wiring
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import (
    CreateNote,
    NoteDocument,
    NotesFilesystemUnavailable,
    NoteUnparseable,
    NotFound,
    PreconditionRequired,
    ReplaceNote,
    StoreError,
    StoreUnavailable,
    VersionConflict,
)

TOKEN = "internal-test-token"
NOTE_ID = "01K4Q8Z3N7V2X9M1B5C6D8E0F2"
NOTE = NoteDocument(
    id=NOTE_ID,
    path="Review/Runbook.md",
    title="Runbook",
    frontmatter={"id": NOTE_ID},
    body="# Runbook\n",
    content_hash="sha256:abc",
    size_bytes=11,
    updated_at=datetime(2026, 9, 8, tzinfo=UTC),
)


class RaisingStore:
    """A store that answers every call with the error it was given."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def get_note(self, note_id: str) -> None:
        raise self.error

    async def replace_note(self, note_id: str, request: ReplaceNote, if_match: str) -> None:
        raise self.error


class OneNoteStore:
    """A store holding exactly one note, so a lookup by any other id misses."""

    def __init__(self, note: NoteDocument) -> None:
        self.note = note
        self.asked_for: list[str] = []

    async def get_note(self, note_id: str) -> NoteDocument:
        self.asked_for.append(note_id)
        if note_id != self.note.id:
            raise NotFound(note_id)
        return self.note

    async def replace_note(self, note_id: str, request: ReplaceNote, if_match: str) -> NoteDocument:
        self.asked_for.append(note_id)
        if note_id != self.note.id:
            raise NotFound(note_id)
        if if_match != self.note.content_hash:
            raise VersionConflict(self.note.content_hash)
        self.note = self.note.model_copy(update={"body": request.body})
        return self.note


def connected(store: RaisingStore | OneNoteStore) -> HttpStoreClient:
    app = FastAPI()
    app.state.auth = InternalAuth(TOKEN)
    app.state.store = store
    app.include_router(internal_router)
    return HttpStoreClient("http://store", TOKEN, transport=httpx.ASGITransport(app=app))


@pytest.mark.parametrize(
    "suffix",
    ["#anything", "?q=1", " and more"],
    ids=["fragment", "query", "space"],
)
async def test_an_identifier_is_never_reparsed_as_part_of_the_url(suffix: str):
    """An id is one path segment. A `#` or `?` in it used to truncate the lookup.

    The store then answered 200 for the note whose id was the prefix, so the
    public surface returned a note under an identifier nobody asked for.
    """
    store = OneNoteStore(NOTE)
    client = connected(store)
    try:
        with pytest.raises(NotFound):
            await client.get_note(NOTE_ID + suffix)
    finally:
        await client.aclose()
    assert store.asked_for == [NOTE_ID + suffix]


async def test_an_identifier_that_is_only_punctuation_is_a_miss_not_an_outage():
    """`?` alone used to build a URL ending in `/notes/`, which redirects.

    The client could not read that redirect as a typed error, so a healthy
    store answered 503 `store_unavailable` next to a 200 `/readyz`.
    """
    client = connected(OneNoteStore(NOTE))
    try:
        with pytest.raises(NotFound):
            await client.get_note("?")
    finally:
        await client.aclose()


async def test_a_missing_note_comes_back_as_the_same_typed_error():
    """The client rebuilds `NotFound` with the identifier, not with the message."""
    client = connected(RaisingStore(NotFound(NOTE_ID)))
    try:
        with pytest.raises(NotFound) as raised:
            await client.get_note(NOTE_ID)
    finally:
        await client.aclose()
    assert raised.value.note_id == NOTE_ID
    assert str(raised.value) == f"no note with id {NOTE_ID}"


async def test_an_unparseable_note_round_trips_without_nesting_its_message():
    reason = "frontmatter has no closing delimiter"
    client = connected(RaisingStore(NoteUnparseable(NOTE_ID, reason)))
    try:
        with pytest.raises(NoteUnparseable) as raised:
            await client.get_note(NOTE_ID)
    finally:
        await client.aclose()
    assert raised.value.note_id == NOTE_ID
    assert raised.value.reason == reason
    assert str(raised.value).count("the frontmatter of note") == 1


async def test_the_internal_surface_keeps_the_cause_the_public_one_hides():
    """The client rebuilds the typed error with the operating system's reason.

    The same error answers the public API with a fixed message, because that
    surface is unauthenticated and the reason names container paths.
    """
    cause = "[Errno 30] Read-only file system: '/data/notes/Review'"
    client = connected(RaisingStore(NotesFilesystemUnavailable(cause)))
    try:
        with pytest.raises(NotesFilesystemUnavailable) as raised:
            await client.get_note(NOTE_ID)
    finally:
        await client.aclose()
    assert str(raised.value) == cause


async def test_a_version_conflict_round_trips_with_the_current_etag():
    """The client hands back the ETag the file has now, so a caller can re-read."""
    client = connected(RaisingStore(VersionConflict("sha256:newer")))
    try:
        with pytest.raises(VersionConflict) as raised:
            await client.replace_note(NOTE_ID, ReplaceNote(), "sha256:abc")
    finally:
        await client.aclose()
    assert raised.value.current_etag == "sha256:newer"


async def test_a_matching_etag_replaces_and_a_stale_one_is_refused_over_the_wire():
    """What the client puts in `If-Match` is what the store compares."""
    client = connected(OneNoteStore(NOTE))
    try:
        replaced = await client.replace_note(NOTE_ID, ReplaceNote(body="# Edited\n"), "sha256:abc")
        assert replaced.body == "# Edited\n"
        with pytest.raises(VersionConflict):
            await client.replace_note(NOTE_ID, ReplaceNote(), "sha256:stale")
    finally:
        await client.aclose()


async def test_an_empty_etag_is_refused_as_a_missing_precondition():
    """A blank `If-Match` is no precondition, and the store says so before writing."""
    client = connected(OneNoteStore(NOTE))
    try:
        with pytest.raises(PreconditionRequired):
            await client.replace_note(NOTE_ID, ReplaceNote(), "")
    finally:
        await client.aclose()


def store_wiring(tmp_path, build_version: str | None = None):
    token = tmp_path / "internal-token"
    token.write_text(f"{TOKEN}\n", encoding="utf-8")
    password = tmp_path / "postgres-password"
    password.write_text("coppermind\n", encoding="utf-8")
    return Wiring(
        data_dir=tmp_path / "data",
        internal_token_file=token,
        database_url="postgresql://coppermind@127.0.0.1:5999/absent",
        db_password_file=password,
        build_version=build_version,
    )


def store_app(tmp_path, build_version: str | None = None):
    return create_app(store_wiring(tmp_path, build_version))


def test_internal_notes_reject_missing_and_wrong_bearer_tokens(tmp_path):
    with TestClient(store_app(tmp_path)) as client:
        for headers in ({}, {"Authorization": "Bearer wrong-token"}):
            response = client.post(
                "/internal/v1/notes",
                headers=headers,
                json={"title": "Unauthorized write"},
            )
            assert response.status_code == 401
            assert response.json() == {
                "error": "unauthorized",
                "message": "a valid internal bearer token is required",
            }


def test_an_unknown_internal_route_answers_in_the_documented_envelope(tmp_path):
    """Routing raises these before any route runs, so they bypass a route handler."""
    with TestClient(store_app(tmp_path)) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        missing = client.get("/internal/v1/nothing-here", headers=headers)
        assert missing.status_code == 404
        assert missing.json()["error"] == "not_found"

        wrong_method = client.delete("/internal/v1/notes", headers=headers)
        assert wrong_method.status_code == 405
        assert wrong_method.json()["error"] == "method_not_allowed"


def test_an_internal_replace_without_if_match_answers_428(tmp_path):
    """The internal surface refuses the same thing the public one does."""
    with TestClient(store_app(tmp_path)) as client:
        response = client.put(
            f"/internal/v1/notes/{NOTE_ID}",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"frontmatter": {}, "body": ""},
        )
    assert response.status_code == 428
    assert response.json()["error"] == "precondition_required"


def test_a_malformed_create_body_answers_as_a_validation_error(tmp_path):
    with TestClient(store_app(tmp_path)) as client:
        response = client.post(
            "/internal/v1/notes",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"body": "no title"},
        )
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert body["errors"] == ["title: Field required"]


def test_healthz_reports_the_version_the_image_was_built_from(tmp_path):
    """CI stamps the tag it built from; a working tree run reports the package version."""
    with TestClient(store_app(tmp_path)) as plain:
        assert plain.get("/healthz").json()["version"] == __version__

    with TestClient(store_app(tmp_path, build_version="v1.2.0")) as stamped:
        assert stamped.get("/healthz").json()["version"] == "v1.2.0"


def broken_settings_app(tmp_path, body: str | None = None):
    """A store whose `settings.yaml` an operator edited into something invalid.

    Editing files under `/data/state` is the only configuration surface this
    slice has, so this is an ordinary operator action rather than an exotic one.
    """
    state = tmp_path / "data" / "state"
    state.mkdir(parents=True)
    (state / "settings.yaml").write_text(
        body or "schema_version: 1\nrevision: 1\nsync:\n  plan: gold\n", encoding="utf-8"
    )
    return create_app(store_wiring(tmp_path))


def test_an_unexpected_error_answers_in_the_documented_envelope(tmp_path):
    """A control file the settings model rejects used to escape as plain text."""
    app = broken_settings_app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/internal/v1/notes",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"title": "During a bad settings edit"},
        )
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"] == "internal_error"


async def test_a_store_that_answered_is_never_reported_unreachable(tmp_path):
    """A 503 saying the store could not be reached, beside a 200 readiness, is a lie."""
    app = broken_settings_app(tmp_path)
    with TestClient(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        client = HttpStoreClient("http://store", TOKEN, transport=transport)
        try:
            with pytest.raises(StoreError) as raised:
                await client.create_note(CreateNote(title="During a bad settings edit"))
        finally:
            await client.aclose()
    assert not isinstance(raised.value, StoreUnavailable)
    assert "sync.plan" in str(raised.value)


def test_a_control_file_the_models_reject_makes_the_store_report_not_ready(tmp_path):
    """A false green is the worst answer: every note operation fails behind it.

    The process itself stays up, so the operator can still read `/healthz` and
    the log to find out which edit did it.
    """
    app = broken_settings_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    control = next(check for check in body["checks"] if check["name"] == "control_state")
    assert control["ok"] is False
    assert "settings.yaml" in control["detail"]
    assert "sync.plan" in control["detail"]


def test_an_unknown_setting_makes_readiness_name_the_rejected_key(tmp_path):
    app = broken_settings_app(
        tmp_path,
        "schema_version: 1\nrevision: 1\nnotes:\n  review_fodler: Inbox\n",
    )
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    control = next(check for check in response.json()["checks"] if check["name"] == "control_state")
    assert "notes.review_fodler" in control["detail"]


def test_a_missing_schema_role_makes_readiness_name_the_role(tmp_path):
    wiring = store_wiring(tmp_path)
    control = ControlState(wiring.state_dir)
    control.ensure_defaults()
    body = dict(control.store.read("schema").body)
    body["roles"].pop("sources_key")
    control.store.write("schema", body, if_revision=1)
    with TestClient(create_app(wiring)) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    control_check = next(
        check for check in response.json()["checks"] if check["name"] == "control_state"
    )
    assert "sources_key" in control_check["detail"]


def test_a_healthy_store_reports_its_control_files_as_ready(tmp_path):
    with TestClient(store_app(tmp_path)) as client:
        body = client.get("/readyz").json()
    control = next(check for check in body["checks"] if check["name"] == "control_state")
    assert control["ok"] is True
    assert control["detail"] == ""
