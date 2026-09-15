"""The public contract, with a stand-in store.

The API is stateless and every note operation is a call to the store, so these
tests replace the store with an object that satisfies the same contract. What
is under test is the mapping: status codes, the ETag, the error envelope, and
the fact that nothing here touches a file.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from coppermind_api import __version__
from coppermind_api import auth as auth_module
from coppermind_api.deps import store
from coppermind_api.main import create_app
from fastapi.testclient import TestClient

from coppermind.api_keys import API_SCOPES, ApiKeySet, create_key
from coppermind.settings import Wiring
from coppermind.store_protocol import (
    CreateNote,
    MetadataUnavailable,
    NoteDocument,
    NotesFilesystemUnavailable,
    NoteUnparseable,
    NotFound,
    ReplaceNote,
    StoreUnavailable,
    ValidationFailed,
    VersionConflict,
)

NOTE = NoteDocument(
    id="01K4Q8Z3N7V2X9M1B5C6D8E0F2",
    path="Review/2026-09-08 Ameren Architecture Sync.md",
    title="Ameren Architecture Sync",
    frontmatter={"id": "01K4Q8Z3N7V2X9M1B5C6D8E0F2", "type": "meeting"},
    body="# Ameren Architecture Sync\n",
    content_hash="sha256:abc",
    size_bytes=42,
    updated_at=datetime(2026, 9, 8, tzinfo=UTC),
)

REPLACED = NOTE.model_copy(
    update={
        "body": "# Ameren Architecture Sync\n\n- Corrected on review\n",
        "content_hash": "sha256:def",
        "size_bytes": 71,
    }
)

KEY_ID = "a1b2c3d4e5f6a7b8"
KEY_SECRET = "unit-test-full-scope-secret"
KEY_RECORD, KEY = create_key(
    "unit test",
    list(API_SCOPES),
    key_id=KEY_ID,
    secret=KEY_SECRET,
    created_at=datetime(2026, 9, 8, tzinfo=UTC),
)
READ_KEY_RECORD, READ_KEY = create_key(
    "read only",
    ["notes:read"],
    key_id="b1c2d3e4f5a6b7c8",
    secret="unit-test-read-only-secret",
    created_at=datetime(2026, 9, 8, tzinfo=UTC),
)


class FakeStore:
    """Satisfies the part of the store contract this slice uses."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.created: CreateNote | None = None
        self.replaced: tuple[str, ReplaceNote, str] | None = None
        self.key_reads = 0
        self.key_records = [KEY_RECORD, READ_KEY_RECORD]

    async def create_note(self, request: CreateNote) -> NoteDocument:
        if self.error:
            raise self.error
        self.created = request
        return NOTE

    async def get_note(self, note_id: str) -> NoteDocument:
        if self.error:
            raise self.error
        return NOTE

    async def replace_note(self, note_id: str, request: ReplaceNote, if_match: str) -> NoteDocument:
        if self.error:
            raise self.error
        self.replaced = (note_id, request, if_match)
        return REPLACED

    async def is_ready(self) -> bool:
        return self.error is None

    async def get_api_keys(self) -> ApiKeySet:
        self.key_reads += 1
        return ApiKeySet(keys=list(self.key_records))


@pytest.fixture
def client(tmp_path: Path):
    token = tmp_path / "internal-token"
    token.write_text("test-token\n", encoding="utf-8")
    app = create_app(Wiring(internal_token_file=token, store_url="http://store.invalid:8081"))
    fake = FakeStore()

    app.dependency_overrides[store] = lambda: fake
    with TestClient(app) as test_client:
        # Readiness reads the client off application state rather than through
        # the dependency, so it is replaced there too.
        app.state.store = fake
        app.state.api_key_auth._store = fake
        test_client.headers.update({"Authorization": f"Bearer {KEY}"})
        yield test_client, fake


def test_health_says_the_process_is_up(client):
    test_client, _ = client
    response = test_client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["service"] == "coppermind-api"


def test_v1_refuses_a_missing_or_malformed_key(client):
    test_client, _ = client
    wrong_secret = f"Bearer cm_{KEY_ID}_wrong-secret"
    for authorization in ("", "Bearer wrong", "Basic credentials", wrong_secret):
        response = test_client.get(f"/v1/notes/{NOTE.id}", headers={"Authorization": authorization})
        assert response.status_code == 401
        assert response.json()["error"] == "unauthorized"
        assert response.headers["www-authenticate"] == "Bearer"


def test_a_key_without_the_needed_scope_is_forbidden(client):
    test_client, fake = client
    response = test_client.post(
        "/v1/notes",
        json={"title": "Not allowed"},
        headers={"Authorization": f"Bearer {READ_KEY}"},
    )
    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"
    assert fake.created is None


async def test_the_cache_expires_so_a_revoked_key_stops_working():
    """The five-minute bound is what limits how long a revoked key survives."""
    fake = FakeStore()
    seconds = 0.0
    authenticator = auth_module.ApiKeyAuthenticator(fake, clock=lambda: seconds)

    assert await authenticator.authenticate(f"Bearer {KEY}") is not None
    assert fake.key_reads == 1

    fake.key_records = [
        KEY_RECORD.model_copy(update={"revoked_at": datetime(2026, 9, 9, tzinfo=UTC)})
    ]
    assert await authenticator.authenticate(f"Bearer {KEY}") is not None
    assert fake.key_reads == 1

    seconds = auth_module.CACHE_TTL_SECONDS + 1
    assert await authenticator.authenticate(f"Bearer {KEY}") is None
    assert fake.key_reads == 2


async def test_a_key_minted_after_the_cache_was_warmed_still_works():
    """Traffic warms the cache; a key minted after it must not wait the cache out."""
    fake = FakeStore()
    seconds = 0.0
    authenticator = auth_module.ApiKeyAuthenticator(fake, clock=lambda: seconds)

    assert await authenticator.authenticate(f"Bearer {KEY}") is not None
    assert fake.key_reads == 1

    late_record, late_key = create_key(
        "minted after the probe",
        ["notes:read"],
        key_id="d1e2f3a4b5c6d7e8",
        secret="unit-test-late-secret",
        created_at=datetime(2026, 9, 8, tzinfo=UTC),
    )
    fake.key_records = [KEY_RECORD, READ_KEY_RECORD, late_record]

    # Inside the floor an unknown id is refused without touching the store.
    assert await authenticator.authenticate(f"Bearer {late_key}") is None
    assert fake.key_reads == 1

    seconds = auth_module.UNKNOWN_KEY_RELOAD_FLOOR_SECONDS
    principal = await authenticator.authenticate(f"Bearer {late_key}")
    assert principal is not None
    assert principal.scopes == frozenset({"notes:read"})
    assert fake.key_reads == 2


class GatedKeyStore(FakeStore):
    """A store whose key read begins, then waits for the test to let it answer."""

    def __init__(self, key_error: Exception | None = None) -> None:
        super().__init__()
        self.key_error = key_error
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def get_api_keys(self) -> ApiKeySet:
        self.key_reads += 1
        answer = ApiKeySet(keys=list(self.key_records))
        self.started.set()
        await self.release.wait()
        if self.key_error is not None:
            raise self.key_error
        return answer


async def test_a_hung_store_costs_one_attempt_for_every_waiting_credential():
    """A store that stops answering must not be asked once per cold credential."""
    fake = GatedKeyStore(StoreUnavailable("the store never answered"))
    authenticator = auth_module.ApiKeyAuthenticator(fake)

    async def attempt() -> str:
        try:
            await authenticator.authenticate(f"Bearer {KEY}")
        except auth_module.AuthenticationUnavailable:
            return "unavailable"
        return "answered"

    waiters = [asyncio.create_task(attempt()) for _ in range(25)]
    await fake.started.wait()
    fake.release.set()

    assert await asyncio.gather(*waiters) == ["unavailable"] * 25
    assert fake.key_reads == 1


async def test_concurrent_unknown_keys_cost_one_store_read_per_floor_window():
    """An unauthenticated flood must not become one store read per request."""
    fake = GatedKeyStore()
    seconds = 0.0
    authenticator = auth_module.ApiKeyAuthenticator(fake, clock=lambda: seconds)
    unknown = "Bearer cm_00000000000000ff_never-minted"

    waiters = [asyncio.create_task(authenticator.authenticate(unknown)) for _ in range(25)]
    await fake.started.wait()
    fake.release.set()

    assert await asyncio.gather(*waiters) == [None] * 25
    assert fake.key_reads == 1


async def test_an_overlapping_load_cannot_restore_a_revoked_key():
    """One load is in flight at a time, so no late answer can overwrite a newer one."""
    fake = GatedKeyStore()
    seconds = 0.0
    authenticator = auth_module.ApiKeyAuthenticator(fake, clock=lambda: seconds)

    first = asyncio.create_task(authenticator.records())
    await fake.started.wait()
    fake.key_records = [
        KEY_RECORD.model_copy(update={"revoked_at": datetime(2026, 9, 9, tzinfo=UTC)}),
        READ_KEY_RECORD,
    ]
    second = asyncio.create_task(authenticator.records())
    await asyncio.sleep(0)
    fake.release.set()
    await asyncio.gather(first, second)

    assert fake.key_reads == 1

    seconds = auth_module.CACHE_TTL_SECONDS + 1
    assert await authenticator.authenticate(f"Bearer {KEY}") is None
    assert fake.key_reads == 2


def test_successful_authentication_is_cached_without_rehashing(client, monkeypatch):
    test_client, fake = client
    real_verify = auth_module.verify_secret
    calls = 0

    def counted_verify(encoded_hash: str, secret: str) -> bool:
        nonlocal calls
        calls += 1
        return real_verify(encoded_hash, secret)

    monkeypatch.setattr(auth_module, "verify_secret", counted_verify)
    assert test_client.get(f"/v1/notes/{NOTE.id}").status_code == 200
    assert test_client.get(f"/v1/notes/{NOTE.id}").status_code == 200
    assert calls == 1
    assert fake.key_reads == 1


def test_health_readiness_and_openapi_need_no_key(client):
    test_client, _ = client
    for path in ("/healthz", "/readyz", "/openapi.json"):
        assert test_client.get(path, headers={"Authorization": ""}).status_code == 200
    document = test_client.get("/openapi.json", headers={"Authorization": ""}).json()
    assert "HTTPBearer" in document["components"]["securitySchemes"]


def api_app(tmp_path: Path, build_version: str | None = None):
    token = tmp_path / "internal-token"
    token.write_text("test-token\n", encoding="utf-8")
    return create_app(
        Wiring(
            internal_token_file=token,
            store_url="http://store.invalid:8081",
            build_version=build_version,
        )
    )


def test_healthz_reports_the_version_the_image_was_built_from(tmp_path: Path):
    """CI stamps the tag it built from; a working tree run reports the package version."""
    with TestClient(api_app(tmp_path)) as plain:
        assert plain.get("/healthz").json()["version"] == __version__

    with TestClient(api_app(tmp_path, build_version="v1.2.0")) as stamped:
        assert stamped.get("/healthz").json()["version"] == "v1.2.0"


def test_readiness_follows_the_store(client):
    test_client, fake = client
    assert test_client.get("/readyz").status_code == 200

    fake.error = MetadataUnavailable("postgres is down")
    response = test_client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["ready"] is False


def test_creating_a_note_answers_201_with_an_etag_and_a_location(client):
    test_client, fake = client
    response = test_client.post("/v1/notes", json={"title": "  Ameren Architecture Sync  "})
    assert response.status_code == 201
    assert response.headers["etag"] == '"sha256:abc"'
    assert response.headers["location"] == f"/v1/notes/{NOTE.id}"
    assert response.json()["path"] == NOTE.path
    assert fake.created is not None and fake.created.title == "Ameren Architecture Sync"


def test_a_note_needs_a_title(client):
    test_client, _ = client
    response = test_client.post("/v1/notes", json={"title": ""})
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert body["errors"] == ["title: String should have at least 1 character"]


def test_a_note_title_cannot_be_only_whitespace(client):
    test_client, _ = client
    response = test_client.post("/v1/notes", json={"title": "   "})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_an_unknown_path_answers_in_the_documented_envelope(client):
    """The envelope is every non-2xx answer, including the ones routing raises."""
    test_client, _ = client
    missing = test_client.get("/v1/nothing-here")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"

    wrong_method = test_client.delete("/healthz")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"] == "method_not_allowed"


def test_reading_a_note_returns_the_document_and_its_etag(client):
    test_client, _ = client
    response = test_client.get(f"/v1/notes/{NOTE.id}")
    assert response.status_code == 200
    assert response.headers["etag"] == '"sha256:abc"'
    assert response.json()["id"] == NOTE.id


def test_a_missing_note_is_a_404_in_the_error_envelope(client):
    test_client, fake = client
    fake.error = NotFound("nope")
    response = test_client.get("/v1/notes/nope")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_a_database_outage_write_uses_operation_neutral_wording(client):
    test_client, fake = client
    fake.error = MetadataUnavailable("connection refused")
    response = test_client.post("/v1/notes", json={"title": "During an outage"})
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "metadata_unavailable"
    assert "if this was a write, its outcome is unknown" in body["message"]


def test_a_database_outage_read_uses_operation_neutral_wording(client):
    test_client, fake = client
    fake.error = MetadataUnavailable("connection refused")
    response = test_client.get(f"/v1/notes/{NOTE.id}")
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "metadata_unavailable"
    assert "if this was a write, its outcome is unknown" in body["message"]


def test_a_filesystem_failure_does_not_blame_the_database(client):
    """A read only volume is its own fault, and readiness says the same half."""
    test_client, fake = client
    fake.error = NotesFilesystemUnavailable("[Errno 30] Read-only file system")
    response = test_client.post("/v1/notes", json={"title": "During a remount"})
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "notes_filesystem_unavailable"
    assert "notes filesystem could not be read or written" in body["message"]
    assert "notes filesystem is unaffected" not in body["message"]
    # The public surface must not repeat an operating system reason that names
    # container paths.
    assert "detail" not in body
    assert "Read-only file system" not in response.text


def test_an_unreachable_store_reports_an_unknown_in_flight_write(client):
    test_client, fake = client
    fake.error = StoreUnavailable("request timed out")
    response = test_client.post("/v1/notes", json={"title": "During a timeout"})
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "store_unavailable"
    assert "if this was a write, its outcome is unknown" in body["message"]
    assert "notes filesystem is untouched" not in body["message"]


def test_an_unreachable_store_read_uses_operation_neutral_wording(client):
    test_client, fake = client
    fake.error = StoreUnavailable("connection refused")
    response = test_client.get(f"/v1/notes/{NOTE.id}")
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "store_unavailable"
    assert "if this was a write, its outcome is unknown" in body["message"]


def test_a_missing_note_reports_the_identifier_it_was_asked_for(client):
    """The message names the id once, not twice, and the envelope carries it."""
    test_client, fake = client
    fake.error = NotFound(NOTE.id)
    response = test_client.get(f"/v1/notes/{NOTE.id}")
    body = response.json()
    assert body["note_id"] == NOTE.id
    assert body["message"] == f"no note with id {NOTE.id}"


def test_a_schema_violation_comes_back_as_422_with_the_reasons(client):
    test_client, fake = client
    fake.error = ValidationFailed(["account: required when context is one of customer"])
    response = test_client.post("/v1/notes", json={"title": "Missing account"})
    assert response.status_code == 422
    assert response.json()["errors"] == ["account: required when context is one of customer"]


def test_an_unparseable_note_never_returns_the_notes_own_text(client):
    """The public envelope carries none of the broken note's frontmatter.

    The parser's reason quotes the lines it choked on, so it is the caller's
    note content. Two things make publishing it wrong: this surface has no
    authentication in this slice and an operator setting can put it on an
    interface, and the error is raised before the identifier guard can run, so
    a stale mirror row can serve lines from a note the caller never named.

    The assertion is on the absence of that text rather than on the presence of
    a particular sentence, so rewording the message later cannot make this test
    pass while the content leaks again.
    """
    test_client, fake = client
    secret_lines = ["account: AcmeCorp Confidential", "tags: [unclosed", "salary_band: L7"]
    reason = (
        'while parsing a flow sequence\n  in "<unicode string>", line 5, column 7:\n'
        "    tags: [unclosed\n          ^ (line: 5)\n"
        "expected ',' or ']', but got ':'\n"
        '  in "<unicode string>", line 6, column 9:\n    account: AcmeCorp Confidential\n'
    )
    fake.error = NoteUnparseable(NOTE.id, reason)

    response = test_client.get(f"/v1/notes/{NOTE.id}")

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "note_unparseable"
    assert body["note_id"] == NOTE.id
    # Nothing of the file's own content reaches the caller, in any field.
    served = response.text
    for line in secret_lines:
        assert line not in served
    assert "reason" not in body
    assert "unicode string" not in served


def test_replacing_a_note_needs_an_if_match_header(client):
    """A write conditional on nothing is refused before the store is asked."""
    test_client, fake = client
    response = test_client.put(f"/v1/notes/{NOTE.id}", json=NOTE.model_dump(mode="json"))
    assert response.status_code == 428
    body = response.json()
    assert body["error"] == "precondition_required"
    assert "If-Match" in body["message"]
    assert fake.replaced is None


def test_replacing_a_note_with_a_current_etag_answers_200_and_the_new_etag(client):
    """The document a read returns is what gets sent back, edited.

    The quotes HTTP puts around an ETag are stripped before the store sees
    it, and the fields that describe the file rather than its content are
    ignored on the way in rather than rejected.
    """
    test_client, fake = client
    edited = NOTE.model_dump(mode="json")
    edited["body"] = REPLACED.body
    edited["frontmatter"]["reviewed"] = True
    response = test_client.put(
        f"/v1/notes/{NOTE.id}", json=edited, headers={"If-Match": '"sha256:abc"'}
    )
    assert response.status_code == 200
    assert response.headers["etag"] == '"sha256:def"'
    assert response.json()["content_hash"] == "sha256:def"
    assert fake.replaced is not None
    note_id, request, if_match = fake.replaced
    assert note_id == NOTE.id
    assert if_match == "sha256:abc"
    assert request.body == REPLACED.body
    assert request.frontmatter["reviewed"] is True


def test_a_stale_etag_answers_409_naming_the_current_version(client):
    test_client, fake = client
    fake.error = VersionConflict("sha256:newer")
    response = test_client.put(
        f"/v1/notes/{NOTE.id}",
        json={"frontmatter": {}, "body": ""},
        headers={"If-Match": '"sha256:abc"'},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "version_conflict"
    assert body["current_version"] == "sha256:newer"
    assert "read it again" in body["message"]


def test_a_replace_during_a_database_outage_is_a_503(client):
    test_client, fake = client
    fake.error = MetadataUnavailable("connection refused")
    response = test_client.put(
        f"/v1/notes/{NOTE.id}",
        json={"frontmatter": {}, "body": ""},
        headers={"If-Match": '"sha256:abc"'},
    )
    assert response.status_code == 503
    assert response.json()["error"] == "metadata_unavailable"


def test_a_replace_body_must_be_the_document_shape(client):
    test_client, fake = client
    response = test_client.put(
        f"/v1/notes/{NOTE.id}",
        json={"frontmatter": "not a mapping", "body": ""},
        headers={"If-Match": '"sha256:abc"'},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert fake.replaced is None


@pytest.mark.parametrize("missing", ["frontmatter", "body"])
def test_a_replace_missing_half_of_the_document_is_refused(client, missing):
    """Leaving the body out of an edit must not erase it, nor the frontmatter."""
    test_client, fake = client
    document = {"frontmatter": dict(NOTE.frontmatter), "body": NOTE.body}
    del document[missing]
    response = test_client.put(
        f"/v1/notes/{NOTE.id}", json=document, headers={"If-Match": '"sha256:abc"'}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert body["errors"] == [f"{missing}: Field required"]
    assert fake.replaced is None
