"""Admin context forms stay authenticated, CSRF guarded and secret-free."""

from coppermind.context_config import ContextConfig, ModelConnection
from services.admin.tests.browser import admin_browser as admin_browser
from services.admin.tests.test_admin import form_token


class ContextStore:
    def __init__(self):
        self.calls = []

    async def context_action(self, action, payload):
        self.calls.append((action, payload))
        if action == "snapshot":
            return {
                "config": ContextConfig(
                    connections=[
                        ModelConnection(
                            id="local",
                            name="Local",
                            provider="compatible",
                            base_url="http://local.example/v1",
                        )
                    ]
                ).model_dump(),
                "calendar": {"imports": []},
                "events": [
                    {
                        "title": "<script>bad()</script>",
                        "calendar_id": "work",
                        "start": "2026-10-14T14:00:00+00:00",
                        "cancelled": False,
                    }
                ],
                "contacts": [],
                "jobs": [],
                "associations": {},
                "watch_status": {},
                "worker_error": "",
                "discovery": {},
                "tests": {},
                "contact_corrections": {},
            }
        if action == "backlog":
            return {"notes": [], "cursor": None}
        return {"saved": True}


def test_calendar_models_and_review_render_and_escape(signed_in):
    client, _ = signed_in
    client.app.state.store = ContextStore()
    calendar = client.get("/admin/context")
    assert calendar.status_code == 200
    assert "&lt;script&gt;" in calendar.text
    assert "9:00 AM CDT" in calendar.text
    assert "14:00:00+00:00" not in calendar.text
    models = client.get("/admin/context?tab=models")
    assert models.status_code == 200
    assert 'type="password"' in models.text
    assert "synthetic" in models.text
    review = client.get("/admin/context?tab=review")
    assert review.status_code == 200
    assert "Send selected notes for enrichment" in review.text


def test_configuration_and_backlog_are_csrf_guarded(signed_in):
    client, _ = signed_in
    fake = ContextStore()
    client.app.state.store = fake
    assert client.post("/v1/admin/context/enqueue", data={}).status_code == 403
    csrf = form_token(client, "/admin/context?tab=review")
    response = client.post(
        "/v1/admin/context/enqueue",
        data={
            "csrf": csrf,
            "note_example": "on",
            "confirm_send": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    action, payload = fake.calls[-1]
    assert action == "enqueue"
    assert payload["note_ids"] == ["example"]
    assert payload["confirmation"] == "Send selected notes for enrichment"
