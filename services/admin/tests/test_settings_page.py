import html
import re
"""Every model field is editable with validated, revisioned form writes."""

import json
from html import escape

import pytest
from pydantic import BaseModel

from coppermind.settings import ProductSettings, read_settings
from coppermind.statefiles import StateStore
from services.admin.tests.browser import admin_browser as admin_browser
from services.admin.tests.test_keys_page import Inputs


def test_every_product_field_has_value_default_and_help(signed_in):
    client, _ = signed_in
    response = client.get("/admin/settings")
    values = Inputs(response.text).values

    def check(model, prefix=""):
        for name, field in type(model).model_fields.items():
            path = f"{prefix}.{name}" if prefix else name
            value = getattr(model, name)
            if isinstance(value, BaseModel):
                check(value, path)
            else:
                text = value if isinstance(value, str) else json.dumps(value)
                assert values[path] == text
                assert f"Default: <code>{escape(text)}</code>" in response.text
                assert field.description and escape(field.description) in response.text

    check(ProductSettings())
    assert "strands existing projections" in response.text


@pytest.mark.parametrize(
    "field,value",
    [
        ("admin.session_hours", "0"),
        ("git.debounce_s", "0"),
        ("git.poll_interval_s", "0"),
        ("git.debounce_s", '"invalid"'),
        ("git.identity_name", "   "),
        ("sync.plan", "unknown"),
        ("sync.device_name", ""),
        ("limits.ingest_max_bytes", "-1"),
        ("notes.dated_types", "not-json"),
    ],
)
def test_invalid_setting_names_field_and_preserves_file(signed_in, field, value):
    client, wiring = signed_in
    path = wiring.state_dir / "settings.yaml"
    before = path.read_bytes()
    form = Inputs(client.get("/admin/settings").text).values
    response = client.post("/v1/admin/settings", data=form | {field: value})
    assert response.status_code == 422
    assert field in response.text and 'class="error"' in response.text
    assert path.read_bytes() == before


def test_git_debounce_and_poll_reject_zero_and_accept_one(signed_in):
    """git.debounce_s and git.poll_interval_s carry the Git helper minimum of 1."""
    client, wiring = signed_in
    form = Inputs(client.get("/admin/settings").text).values
    # 0 is refused, naming the field and the minimum.
    for field in ("git.debounce_s", "git.poll_interval_s"):
        response = client.post("/v1/admin/settings", data=form | {field: "0"})
        assert response.status_code == 422
        # The rejection paragraph itself names the field and the Git helper's minimum;
        # the surrounding form always contains the field names and stray "1"s.
        errors = re.findall(r'<p class="error">(.*?)</p>', response.text, re.S)
        assert errors, response.text[:400]
        assert f"{field}: Input should be greater than or equal to 1" in html.unescape(errors[0])
    # 1 is accepted (the minimum).
    # Fetch a fresh form to get the current revision.
    fresh_form = Inputs(client.get("/admin/settings").text).values
    for field in ("git.debounce_s", "git.poll_interval_s"):
        post_data = dict(fresh_form)
        post_data[field] = "1"
        response = client.post("/v1/admin/settings", data=post_data)
        assert response.status_code == 200, f"{field}: {response.status_code} {response.text[:200]}"
        assert "Saved revision" in response.text
        fresh_form = Inputs(response.text).values  # refresh after each save


def test_valid_save_updates_file_revision_and_refuses_stale_form(signed_in):
    client, wiring = signed_in
    store = StateStore(wiring.state_dir)
    form = Inputs(client.get("/admin/settings").text).values
    revision = store.read("settings").revision
    response = client.post(
        "/v1/admin/settings",
        data=form
        | {
            "git.debounce_s": "17",
            "git.enabled": "false",
            "sync.max_file_bytes": "123456",
            "notes.dated_types": '["journal"]',
        },
    )
    assert response.status_code == 200
    assert f"Saved revision {revision + 1}" in response.text
    assert store.read("settings").revision == revision + 1
    settings = read_settings(store)
    assert settings.git.debounce_s == 17
    assert settings.git.enabled is False
    assert settings.sync.max_file_bytes == 123456
    assert settings.notes.dated_types == ["journal"]
    assert Inputs(client.get("/admin/settings").text).values["git.debounce_s"] == "17"
    before = store.path_for("settings").read_bytes()
    stale = client.post("/v1/admin/settings", data=form)
    assert stale.status_code == 409
    assert f"revision {revision + 1}" in stale.text
    assert store.path_for("settings").read_bytes() == before


def test_correcting_invalid_stale_form_cannot_overwrite_another_tab(signed_in):
    client, wiring = signed_in
    form = Inputs(client.get("/admin/settings").text).values
    assert (
        client.post("/v1/admin/settings", data=form | {"git.debounce_s": "19"}).status_code == 200
    )
    invalid = client.post("/v1/admin/settings", data=form | {"admin.session_hours": "0"})
    assert invalid.status_code == 422
    corrected = Inputs(invalid.text).values | {"admin.session_hours": "12"}
    assert corrected["revision"] == form["revision"]
    assert client.post("/v1/admin/settings", data=corrected).status_code == 409
    assert read_settings(StateStore(wiring.state_dir)).git.debounce_s == 19
