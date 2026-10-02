import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from coppermind_api.main import create_app
from coppermind_api.settings import Wiring
from coppermind_api.deps import store
from coppermind.store_protocol import SchemaResponse, TagCount
from services.api.tests.test_notes_api import FakeStore, KEY

class FakeStoreWithSchema(FakeStore):
    async def get_schema(self) -> SchemaResponse:
        return SchemaResponse(
            schema_doc={
                "schema_version": 1,
                "keys": [
                    {
                        "name": "type",
                        "kind": "string",
                        "vocabulary": {"note": "A note"},
                        "guidance": "Select type"
                    }
                ],
                "roles": {},
                "tags": {
                    "open": True,
                    "meanings": {},
                    "aliases": {"alias": "canonical"}
                }
            },
            tag_counts=[TagCount(tag="canonical", count=1)]
        )

@pytest.fixture
def client(tmp_path: Path):
    token = tmp_path / "internal-token"
    token.write_text("test-token\n", encoding="utf-8")
    app = create_app(Wiring(internal_token_file=token, store_url="http://store.invalid:8081"))
    fake = FakeStoreWithSchema()

    app.dependency_overrides[store] = lambda: fake
    with TestClient(app) as test_client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        test_client.headers.update({"Authorization": f"Bearer {KEY}"})
        yield test_client, fake

def test_get_schema(client):
    test_client, _ = client
    response = test_client.get("/v1/schema")
    assert response.status_code == 200
    data = response.json()
    assert "fields" in data
    assert "allowed_values" in data
    assert "tags" in data
    assert "aliases" in data
    assert "guidance" in data
    assert "open_tags" in data
    
    assert data["fields"][0]["name"] == "type"
    assert data["allowed_values"]["type"]["note"] == "A note"
    assert data["tags"][0]["tag"] == "canonical"
    assert data["tags"][0]["count"] == 1
    assert data["aliases"]["alias"] == "canonical"
    assert data["guidance"]["type"] == "Select type"
    assert data["open_tags"] is True
