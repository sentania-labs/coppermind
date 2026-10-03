# ruff: noqa
from services.api.tests.test_notes_api import READ_KEY, client


def test_get_schema_success(client):
    test_client, fake = client
    response = test_client.get("/v1/schema", headers={"Authorization": f"Bearer {READ_KEY}"})
    assert response.status_code == 200
    data = response.json()
    assert "keys" in data
    assert "roles" in data
    assert "tag_mode" in data


def test_get_schema_requires_auth(client):
    test_client, fake = client
    response = test_client.get("/v1/schema", headers={"Authorization": ""})
    assert response.status_code == 401
