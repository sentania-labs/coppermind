import pytest
from bs4 import BeautifulSoup
from services.admin.tests.test_keys_page import Inputs
from coppermind.statefiles import StateStore

def test_fields_page_renders_and_shows_tags(signed_in):
    client, wiring = signed_in
    response = client.get("/admin/fields")
    assert response.status_code == 200
    assert "Fields and Tags" in response.text
    # Should see fields like type, context
    assert "type" in response.text
    assert "context" in response.text

def test_fields_save_with_revision_and_stale_refused(signed_in):
    client, wiring = signed_in
    store = StateStore(wiring.state_dir)
    form = Inputs(client.get("/admin/fields").text).values
    revision = store.read("schema").revision
    
    # Save a modification
    form["keys.0.guidance"] = "Changed guidance"
    response = client.post("/v1/admin/schema", data=form)
    assert response.status_code == 200
    assert f"Saved revision {revision + 1}" in response.text
    
    # Stale revision fails
    response2 = client.post("/v1/admin/schema", data=form)
    assert response2.status_code == 409
    assert f"revision {revision + 1}" in response2.text

def test_fields_invalid_field_refused(signed_in):
    client, wiring = signed_in
    form = Inputs(client.get("/admin/fields").text).values
    
    # Invalid JSON
    form["tags.meanings"] = "not json"
    response = client.post("/v1/admin/schema", data=form)
    assert response.status_code == 422
    assert "must be JSON" in response.text
