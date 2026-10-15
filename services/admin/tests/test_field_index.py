"""Field readiness, invalid values and authenticated rebuild controls."""

from services.admin.tests.browser import admin_browser as admin_browser
from services.admin.tests.test_admin import form_token


class FieldStore:
    def __init__(self):
        self.calls = []

    async def field_index_action(self, action, offset=0):
        self.calls.append((action, offset))
        return dict(
            job=dict(state="succeeded", processed=4),
            fields=[dict(name="people", kind="list", ready=True)],
            invalid_count=1,
            invalid=[dict(path="<unsafe>.md", field="people", invalid_reason="expected list")],
            next_offset=None,
        )


def test_status_and_csrf(signed_in):
    client, _ = signed_in
    store = FieldStore()
    client.app.state.store = store
    page = client.get("/admin/field-index")
    assert page.status_code == 200
    assert "Ready" in page.text and "Invalid stored values (1)" in page.text
    assert "&lt;unsafe&gt;.md" in page.text
    assert client.post("/v1/admin/field-index/rebuild").status_code == 403
    token = form_token(client, "/admin/field-index")
    assert (
        client.post(
            "/v1/admin/field-index/rebuild", data={"csrf": token}, follow_redirects=False
        ).status_code
        == 303
    )
    assert store.calls[-1] == ("rebuild", 0)
