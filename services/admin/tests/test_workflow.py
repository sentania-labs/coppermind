"""The normal property and workflow paths work without editing raw schema fields."""

import io
import zipfile

from services.admin.tests.browser import admin_browser as admin_browser
from services.admin.tests.test_admin import form_token
from services.admin.tests.test_context_page import ContextStore


def test_property_creation_is_independent_and_conflicts_keep_user_input(signed_in):
    client, wiring = signed_in
    page = client.get("/admin/fields")
    assert "Note properties" in page.text
    assert "key-0-kind" not in page.text
    token = form_token(client, "/admin/properties/new?preset=attendees")
    form = {
        "csrf": token,
        "revision": "1",
        "name": "attendees",
        "kind": "list",
        "description": "Confirmed people",
        "choices": "",
    }
    saved = client.post("/v1/admin/property", data=form)
    assert saved.status_code == 200
    assert "attendees" in saved.text
    conflict = client.post("/v1/admin/property", data=dict(form, name="invitees"))
    assert conflict.status_code == 422
    assert "another session" in conflict.text
    assert 'value="invitees"' in conflict.text
    schema = client.app.state.control.read("schema")
    assert not any(k["name"] == "invitees" for k in schema.body["keys"])


def test_workflow_setup_and_plugin_download_require_authentication(signed_in):
    client, _ = signed_in
    fake = ContextStore()
    client.app.state.store = fake
    for tab in ("setup", "enrichment", "intake", "review", "dossiers"):
        response = client.get("/admin/workflow?tab=" + tab)
        assert response.status_code == 200
    review = client.get("/admin/workflow?tab=review")
    assert "Finish enrichment setup" in review.text
    assert "Enrich selected notes" not in review.text
    assert client.post("/v1/admin/context/setup-lifecycle", data={}).status_code == 403
    download = client.get("/admin/workflow/plugin.zip")
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert set(archive.namelist()) == {
            "coppermind-review/" + name for name in ("main.js", "manifest.json", "styles.css")
        }
    token = form_token(client, "/admin/workflow")
    client.post("/v1/admin/logout", data={"csrf": token})
    assert client.get("/admin/workflow/plugin.zip", follow_redirects=False).status_code == 303


def test_property_editor_rejects_invalid_names_and_choice_rules(signed_in):
    client, _ = signed_in
    token = form_token(client, "/admin/properties/new")
    before = client.app.state.control.read("schema")
    for name, kind, choices in [
        ("", "string", ""),
        ("bad name", "string", ""),
        ("1invalid", "string", ""),
        ("priority", "enum", ""),
        ("attendees", "list", "unexpected choice"),
    ]:
        response = client.post(
            "/v1/admin/property",
            data={
                "csrf": token,
                "revision": str(before.revision),
                "name": name,
                "kind": kind,
                "choices": choices,
            },
        )
        assert response.status_code == 422
        after = client.app.state.control.read("schema")
        assert after.revision == before.revision
        assert after.body == before.body
        assert 'name="name"' in response.text


class ReviewStore:
    def __init__(self, kind):
        self.kind = kind
        self.patches = []

    async def context_action(self, action, payload):
        return {
            "note": {
                "title": "Work review",
                "body": "Synthetic note.",
                "content_hash": "hash",
                "frontmatter": {"type": self.kind, "date": "2026-10-20", "reviewed": False},
            },
            "roles": {"date_key": "date", "type_key": "type", "reviewed_key": "reviewed"},
            "stale": False,
            "job_id": None,
            "suggestions": [],
        }

    async def patch_frontmatter(self, identity, changes, *, if_match):
        self.patches.append((identity, changes, if_match))


def test_work_review_time_controls_and_save(signed_in):
    client, _ = signed_in
    store = ReviewStore("work")
    client.app.state.store = store
    response = client.get("/admin/review/example")
    assert response.status_code == 200
    assert 'name="time"' in response.text
    assert 'name="timezone"' in response.text
    csrf = form_token(client, "/admin/review/example")
    values = {
        "csrf": csrf,
        "note_id": "example",
        "if_match": "hash",
        "date": "2026-10-20",
        "note_type": "work",
        "time": "09:30",
        "timezone": "America/Chicago",
    }
    response = client.post(
        "/v1/admin/workflow/save-properties", data=values, follow_redirects=False
    )
    assert response.status_code == 303
    assert store.patches[-1][1].set["time"] == "09:30"
    assert store.patches[-1][1].set["timezone"] == "America/Chicago"
    client.post("/v1/admin/workflow/save-properties", data=values | {"time_unknown": "on"})
    assert store.patches[-1][1].set["time_unknown"] is True
    assert store.patches[-1][1].unset == ["time", "timezone"]
    for kind in ("personal", "reference", "journal"):
        store.kind = kind
        response = client.get("/admin/review/example")
        assert response.status_code == 200
        assert 'name="time"' not in response.text
