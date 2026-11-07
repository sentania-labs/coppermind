"""Entity access and search kinds are separately scoped, with no automatic key upgrade."""

from coppermind_api.deps import store
from coppermind_api.main import create_app
from fastapi.testclient import TestClient

from coppermind.api_keys import DEFAULT_SCOPES, ApiKeySet, create_key
from coppermind.settings import Wiring
from coppermind.store_protocol import Page


class EntityStore:
    def __init__(self):
        self.reader, self.reader_key = create_key("Notes", ["notes:read"])
        self.writer, self.writer_key = create_key(
            "Entities", ["entities:read", "entities:write", "notes:read"]
        )
        self.bare, self.bare_key = create_key("Bare", ["entities:read", "entities:write"])
        self.calls = []
        self.searches = []

    async def get_api_keys(self):
        return ApiKeySet(keys=[self.reader, self.writer, self.bare])

    async def entity_action(self, action, payload):
        self.calls.append((action, payload))
        return {"items": [], "next_cursor": None} if action == "list" else {"id": "person"}

    async def search(self, query, *, entities=False):
        self.searches.append(entities)
        return Page(items=[])


def test_entity_scopes_and_search_exclude_ungranted_kinds(tmp_path):
    assert "entities:read" not in DEFAULT_SCOPES and "entities:write" not in DEFAULT_SCOPES
    token = tmp_path / "internal"
    token.write_text("test-internal")
    app = create_app(Wiring(internal_token_file=token))
    fake = EntityStore()
    app.dependency_overrides[store] = lambda: fake
    with TestClient(app) as client:
        app.state.api_key_auth._store = fake
        notes = {"Authorization": f"Bearer {fake.reader_key}"}
        entities = {"Authorization": f"Bearer {fake.writer_key}"}
        bare = {"Authorization": f"Bearer {fake.bare_key}"}
        assert client.get("/v1/entities", headers=bare).status_code == 403
        assert client.get("/v1/entities/person", headers=bare).status_code == 403
        assert (
            client.post(
                "/v1/entities/person",
                headers=bare,
                json={"action": "rename", "version": 1, "name": "New"},
            ).status_code
            == 403
        )
        assert client.get("/v1/entities").status_code == 401
        assert client.get("/v1/entities", headers=notes).status_code == 403
        assert client.get("/v1/entities/person", headers=notes).status_code == 403
        assert (
            client.post(
                "/v1/entities", headers=notes, json={"kind": "person", "name": "Casey"}
            ).status_code
            == 403
        )
        assert client.get("/v1/entities?kind=company&limit=2", headers=entities).status_code == 200
        assert client.get("/v1/entities/person", headers=entities).status_code == 200
        assert (
            client.post(
                "/v1/entities/person",
                headers=entities,
                json={"action": "rename", "version": 1, "name": "New"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/v1/entities/person", headers=entities, json={"action": "rename", "name": "New"}
            ).status_code
            == 422
        )
        assert client.get("/v1/search?q=Casey", headers=notes).status_code == 200
        assert fake.searches == [False]
        # Public query parameters cannot impersonate the internal scope headers.
        assert client.get("/v1/search?q=Casey&entities=true", headers=notes).status_code == 422
        assert client.get("/v1/sources/source", headers=entities).status_code == 403
