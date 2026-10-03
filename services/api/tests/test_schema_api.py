"""`GET /v1/schema` serves the rules the store enforces.

The API has no state volume, so the schema has to come over the store
contract. The end to end test runs the real internal router and the real
client over an in process transport, writes the schema file the way Admin
does, and reads the change back through the public route.
"""

from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from coppermind_api.deps import store
from coppermind_api.main import create_app
from coppermind_store.auth import InternalAuth
from coppermind_store.control import ControlState
from coppermind_store.internal_api import router as internal_router
from coppermind_store.notes import LocalStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coppermind.api_keys import ApiKeySet
from coppermind.schema import default_schema
from coppermind.settings import Wiring
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import SchemaDocument, StoreError, StoreUnavailable
from services.api.tests.test_notes_api import (
    KEY,
    KEY_RECORD,
    READ_KEY,
    READ_KEY_RECORD,
    WRITE_KEY,
    WRITE_KEY_RECORD,
    FakeStore,
)
from services.api.tests.test_notes_api import client as client

TOKEN = "schema-test-token"


class SchemaStore(FakeStore):
    def __init__(self, document: SchemaDocument | None = None) -> None:
        super().__init__()
        self.document = document or SchemaDocument(revision=1, frontmatter_schema=default_schema())
        self.schema_reads = 0

    async def get_schema(self) -> SchemaDocument:
        self.schema_reads += 1
        if self.error:
            raise self.error
        return self.document


def edited_schema() -> SchemaDocument:
    schema = default_schema()
    context = next(definition for definition in schema.keys if definition.name == "context")
    context.vocabulary_meanings = {"customer": "Work for a paying customer."}
    context.guidance = "Pick where the note files."
    schema.tags = {"architecture": "System design decisions.", "runbook": ""}
    schema.tag_aliases = {"arch": "architecture", "design": "architecture"}
    schema.tag_mode = "closed"
    return SchemaDocument(revision=4, frontmatter_schema=schema)


@pytest.fixture
def schema_client(client):
    test_client, _ = client
    fake = SchemaStore(edited_schema())
    test_client.app.dependency_overrides[store] = lambda: fake
    test_client.app.state.store = fake
    test_client.app.state.api_key_auth._store = fake
    return test_client, fake


def test_a_read_key_gets_fields_allowed_values_tags_aliases_and_guidance(schema_client):
    test_client, fake = schema_client
    response = test_client.get("/v1/schema", headers={"Authorization": f"Bearer {READ_KEY}"})
    assert response.status_code == 200
    body = response.json()
    assert body["revision"] == 4
    assert body["schema_version"] == 1
    fields = {field["name"]: field for field in body["fields"]}
    assert [field["name"] for field in body["fields"]] == [k.name for k in default_schema().keys]
    assert fields["context"]["kind"] == "enum"
    assert fields["context"]["required"] is True
    assert fields["context"]["guidance"] == "Pick where the note files."
    assert fields["context"]["allowed_values"] == [
        {"value": "customer", "meaning": "Work for a paying customer."},
        {"value": "internal", "meaning": ""},
        {"value": "external", "meaning": ""},
        {"value": "personal", "meaning": ""},
    ]
    assert fields["account"]["required_when"] == {"key": "context", "values": ["customer"]}
    assert fields["date"]["required_when"] is None
    assert fields["tags"]["allowed_values"] == []
    assert body["roles"]["tags_key"] == "tags"
    assert body["tags"] == {
        "mode": "closed",
        "listed": [
            {
                "tag": "architecture",
                "meaning": "System design decisions.",
                "aliases": ["arch", "design"],
            },
            {"tag": "runbook", "meaning": "", "aliases": []},
        ],
        "aliases": {"arch": "architecture", "design": "architecture"},
    }
    assert fake.schema_reads == 1


def test_the_schema_needs_notes_read(schema_client):
    test_client, fake = schema_client
    response = test_client.get("/v1/schema", headers={"Authorization": f"Bearer {WRITE_KEY}"})
    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"
    assert fake.schema_reads == 0


def test_the_schema_needs_a_key(schema_client):
    test_client, fake = schema_client
    response = test_client.get("/v1/schema", headers={"Authorization": ""})
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"
    assert fake.schema_reads == 0


def test_a_store_that_cannot_answer_is_a_503_not_a_default_schema(schema_client):
    test_client, fake = schema_client
    fake.error = StoreUnavailable("connection refused")
    response = test_client.get("/v1/schema", headers={"Authorization": f"Bearer {KEY}"})
    assert response.status_code == 503
    assert response.json()["error"] == "store_unavailable"
    assert "fields" not in response.json()


def test_an_unreadable_schema_file_is_an_error_not_a_default_schema(schema_client):
    test_client, fake = schema_client
    fake.error = StoreError("the frontmatter schema could not be read: /data/state/schema.yaml")
    response = test_client.get("/v1/schema", headers={"Authorization": f"Bearer {KEY}"})
    assert response.status_code == 500
    assert "/data/state" not in response.text


def connected_api(tmp_path: Path) -> tuple[TestClient, ControlState]:
    """The API in front of a real store contract reading a real state directory."""
    control = ControlState(tmp_path / "state")
    control.ensure_defaults()
    keys = ApiKeySet(keys=[KEY_RECORD, READ_KEY_RECORD, WRITE_KEY_RECORD])
    control.store.write("keys", keys.model_dump(mode="json"), if_revision=None)
    local = LocalStore(tmp_path / "notes", control, cast(Any, object()), tmp_path / "sources")
    store_app = FastAPI()
    store_app.state.auth = InternalAuth(TOKEN)
    store_app.state.store = local
    store_app.include_router(internal_router)
    remote = HttpStoreClient("http://store", TOKEN, transport=httpx.ASGITransport(app=store_app))

    token = tmp_path / "internal-token"
    token.write_text(f"{TOKEN}\n", encoding="utf-8")
    app = create_app(Wiring(internal_token_file=token, store_url="http://store.invalid:8081"))
    app.dependency_overrides[store] = lambda: remote
    test_client = TestClient(app)
    test_client.__enter__()
    app.state.store = remote
    app.state.api_key_auth._store = remote
    return test_client, control


def test_an_admin_edit_to_the_schema_file_reaches_api_callers(tmp_path):
    """The rules served are the persisted ones, never a bundled default."""
    test_client, control = connected_api(tmp_path)
    try:
        headers = {"Authorization": f"Bearer {READ_KEY}"}
        before = test_client.get("/v1/schema", headers=headers)
        assert before.status_code == 200
        assert before.json()["revision"] == 1
        assert before.json()["tags"] == {"mode": "open", "listed": [], "aliases": {}}

        state = control.store.read("schema")
        body = dict(state.body)
        body["tags"] = {"work": "Anything for the day job."}
        body["tag_aliases"] = {"job": "work"}
        body["tag_mode"] = "closed"
        control.store.write("schema", body, if_revision=state.revision)

        after = test_client.get("/v1/schema", headers=headers)
        assert after.status_code == 200
        assert after.json()["revision"] == 2
        assert after.json()["tags"] == {
            "mode": "closed",
            "listed": [{"tag": "work", "meaning": "Anything for the day job.", "aliases": ["job"]}],
            "aliases": {"job": "work"},
        }
    finally:
        test_client.__exit__(None, None, None)


def test_a_broken_schema_file_is_refused_over_the_contract(tmp_path):
    test_client, control = connected_api(tmp_path)
    try:
        control.store.path_for("schema").write_text("revision: 3\nkeys: [\n", encoding="utf-8")
        response = test_client.get("/v1/schema", headers={"Authorization": f"Bearer {KEY}"})
        assert response.status_code == 500
        assert response.json()["error"] == "internal_error"
    finally:
        test_client.__exit__(None, None, None)
