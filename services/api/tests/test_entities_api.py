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


def fact(identity, quote, source_versions):
    return {
        "id": identity,
        "predicate": "role",
        "value": "Engineer",
        "evidence": {
            "note_id": "note",
            "note_hash": "hash",
            "path": "Meetings/Note.md",
            "quote": quote,
            "source_versions": source_versions,
        },
    }


ENTITY = {
    "id": "person",
    "facts": [
        fact("from-note", "Casey is an engineer.", {}),
        fact("from-transcript", "I run the platform team.", {"source": 1}),
    ],
}


class QuoteStore(EntityStore):
    def __init__(self):
        super().__init__()
        self.narrow, self.narrow_key = create_key("Narrow", ["entities:read", "notes:read"])
        self.wide, self.wide_key = create_key(
            "Wide", ["entities:read", "notes:read", "sources:read"]
        )

    async def get_api_keys(self):
        return ApiKeySet(keys=[self.narrow, self.wide])

    async def entity_action(self, action, payload):
        return {"items": [ENTITY], "next_cursor": None} if action == "list" else ENTITY


def test_transcript_quotes_need_sources_read(tmp_path):
    token = tmp_path / "internal"
    token.write_text("test-internal")
    app = create_app(Wiring(internal_token_file=token))
    fake = QuoteStore()
    app.dependency_overrides[store] = lambda: fake
    with TestClient(app) as client:
        app.state.api_key_auth._store = fake
        narrow = {"Authorization": f"Bearer {fake.narrow_key}"}
        wide = {"Authorization": f"Bearer {fake.wide_key}"}
        for path in ("/v1/entities/person", "/v1/entities"):
            body = client.get(path, headers=narrow).json()
            facts = (body["items"][0] if "items" in body else body)["facts"]
            note_fact, transcript_fact = facts
            assert note_fact["evidence"]["quote"] == "Casey is an engineer."
            assert "quote_withheld" not in note_fact["evidence"]
            assert transcript_fact["evidence"]["quote"] == ""
            assert transcript_fact["evidence"]["quote_withheld"] is True
            assert transcript_fact["value"] == "Engineer"
            assert transcript_fact["evidence"]["source_versions"] == {"source": 1}
            assert "run the platform" not in client.get(path, headers=narrow).text
            body = client.get(path, headers=wide).json()
            facts = (body["items"][0] if "items" in body else body)["facts"]
            assert facts[1]["evidence"]["quote"] == "I run the platform team."
            assert "quote_withheld" not in facts[1]["evidence"]


def test_voice_consent_and_settings_require_people_write_scope(tmp_path):
    token = tmp_path / "internal"
    token.write_text("test-internal")
    app = create_app(Wiring(internal_token_file=token))
    fake = EntityStore()
    fake.bare, reader_key = create_key("People reader", ["notes:read", "entities:read"])
    app.dependency_overrides[store] = lambda: fake
    with TestClient(app) as client:
        app.state.api_key_auth._store = fake
        read = {"Authorization": f"Bearer {reader_key}"}
        write = {"Authorization": f"Bearer {fake.writer_key}"}
        assert client.get("/v1/entities/person/voice", headers=read).status_code == 200
        assert (
            client.put(
                "/v1/entities/person/voice", headers=read, json={"enabled": True}
            ).status_code
            == 403
        )
        assert client.delete("/v1/entities/person/voice", headers=read).status_code == 403
        assert (
            client.put(
                "/v1/entities/voice/settings", headers=read, json={"strong": 0.6, "maybe": 0.45}
            ).status_code
            == 403
        )
        assert (
            client.put(
                "/v1/entities/person/voice", headers=write, json={"enabled": True}
            ).status_code
            == 200
        )
        assert client.delete("/v1/entities/person/voice", headers=write).status_code == 200
        assert (
            client.put(
                "/v1/entities/voice/settings", headers=write, json={"strong": 0.6, "maybe": 0.45}
            ).status_code
            == 200
        )
        assert ("voice-set", {"id": "person", "enabled": True}) in fake.calls
        assert ("voice-delete", {"id": "person"}) in fake.calls
