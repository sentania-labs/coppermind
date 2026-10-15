"""Calendar upload keeps authentication, size limits and raw bytes intact."""

import base64

from fastapi.testclient import TestClient

from services.api.tests.test_ingest_api import client_for


def test_raw_calendar_upload_requires_source_write_and_preserves_bytes(tmp_path):
    app, fake = client_for(tmp_path)
    calls = []

    async def context_action(action, payload):
        calls.append((action, payload))
        return {"id": "receipt", "state": "pending"}

    fake.context_action = context_action
    with TestClient(app) as client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        url = "/v1/calendars/work/imports?filename=export.ics"
        assert client.post(url, content=b"calendar").status_code == 401
        assert (
            client.post(
                url,
                content=b"calendar",
                headers={
                    "Authorization": f"Bearer {fake.notes_key}",
                },
            ).status_code
            == 403
        )
        payload = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
        response = client.post(
            url,
            content=payload,
            headers={
                "Authorization": f"Bearer {fake.sources_key}",
                "Content-Type": "text/calendar",
            },
        )
        assert response.status_code == 202
        assert base64.b64decode(calls[0][1]["content_base64"]) == payload
        assert (
            client.post(
                url,
                content=b"x" * (10 * 1024**2 + 1),
                headers={
                    "Authorization": f"Bearer {fake.sources_key}",
                },
            ).status_code
            == 413
        )
        assert len(calls) == 1
