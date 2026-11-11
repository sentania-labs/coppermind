"""`/v1/recordings`: hand in a stored recording and follow it until its note exists."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from coppermind.api_keys import ApiKeySet, create_key
from coppermind.store_protocol import ValidationFailed
from services.api.tests.test_ingest_api import client_for

ARTIFACT_ID = "01K4Q8Z2A0P1Q2R3S4T5U6V7W8"
BODY = {
    "artifact_id": ARTIFACT_ID,
    "provider": "n8n",
    "external_source_id": "plaud/rec-1",
    "title": "Standup",
    "captured_at": "2026-11-09T09:30:00-06:00",
}


def status(state="queued", **extra):
    return {
        "id": "a" * 64,
        "state": state,
        "artifact_id": ARTIFACT_ID,
        "attempts": 0,
        "attempt_limit": 2,
        "created_at": "2026-11-09T15:30:00+00:00",
        **extra,
    }


def serve(tmp_path: Path, answer):
    app, fake = client_for(tmp_path)
    calls = []
    created = datetime(2026, 9, 8, tzinfo=UTC)
    fake.full_record, fake.full_key = create_key(
        "recorder", ["sources:write", "notes:write", "artifacts:read"], created_at=created
    )
    fake.unreadable_record, fake.unreadable_key = create_key(
        "writes but cannot read artifacts", ["sources:write", "notes:write"], created_at=created
    )

    async def keys():
        return ApiKeySet(
            keys=[fake.full_record, fake.sources_record, fake.notes_record, fake.unreadable_record]
        )

    fake.get_api_keys = keys

    async def context_action(action, payload):
        calls.append((action, payload))
        if isinstance(answer, Exception):
            raise answer
        return answer

    fake.context_action = context_action
    return app, fake, calls


def test_a_recording_needs_all_three_scopes_and_is_accepted_for_later(tmp_path):
    app, fake, calls = serve(tmp_path, status())
    with TestClient(app) as client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        assert client.post("/v1/recordings", json=BODY).status_code == 401
        for key in (fake.sources_key, fake.notes_key, fake.unreadable_key):
            denied = client.post(
                "/v1/recordings", json=BODY, headers={"Authorization": f"Bearer {key}"}
            )
            assert denied.status_code == 403
        assert not calls
        response = client.post(
            "/v1/recordings", json=BODY, headers={"Authorization": f"Bearer {fake.full_key}"}
        )
    assert response.status_code == 202
    assert response.json()["state"] == "queued"
    action, payload = calls[0]
    assert action == "recording" and payload["artifact_id"] == ARTIFACT_ID
    assert payload["external_source_id"] == "plaud/rec-1"


def test_a_settled_intake_answers_200_so_a_retry_can_tell_it_is_done(tmp_path):
    done = status("succeeded", note_id="n1", note_path="Review/Standup.md", source_id="s1")
    app, fake, _ = serve(tmp_path, done)
    with TestClient(app) as client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        response = client.post(
            "/v1/recordings", json=BODY, headers={"Authorization": f"Bearer {fake.full_key}"}
        )
    assert response.status_code == 200 and response.json()["note_path"] == "Review/Standup.md"


def test_the_plain_refusal_from_the_store_reaches_the_caller_as_a_validation_error(tmp_path):
    message = "No transcription service is set up"
    app, fake, _ = serve(tmp_path, ValidationFailed([message]))
    with TestClient(app) as client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        response = client.post(
            "/v1/recordings", json=BODY, headers={"Authorization": f"Bearer {fake.full_key}"}
        )
    assert response.status_code == 422 and message in response.text


def test_unknown_fields_and_naive_times_are_refused_before_the_store_is_asked(tmp_path):
    app, fake, calls = serve(tmp_path, status())
    with TestClient(app) as client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        headers = {"Authorization": f"Bearer {fake.full_key}"}
        assert (
            client.post("/v1/recordings", json={**BODY, "speed": "fast"}, headers=headers)
        ).status_code == 422
        assert (
            client.post(
                "/v1/recordings",
                json={**BODY, "captured_at": "2026-11-09T09:30:00"},
                headers=headers,
            )
        ).status_code == 422
    assert not calls


def test_status_needs_sources_read(tmp_path):
    app, fake, calls = serve(tmp_path, status("running"))
    created = datetime(2026, 11, 9, tzinfo=UTC)
    reader_record, reader_key = create_key("reader", ["sources:read"], created_at=created)

    async def keys():
        return ApiKeySet(keys=[fake.notes_record, reader_record])

    fake.get_api_keys = keys
    url = f"/v1/recordings/{'a' * 64}"
    with TestClient(app) as client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        assert client.get(url).status_code == 401
        denied = client.get(url, headers={"Authorization": f"Bearer {fake.notes_key}"})
        assert denied.status_code == 403 and not calls
        allowed = client.get(url, headers={"Authorization": f"Bearer {reader_key}"})
    assert allowed.status_code == 200 and allowed.json()["state"] == "running"
    assert calls == [("recording-status", {"id": "a" * 64})]


def test_waiting_and_retry_require_their_scopes(tmp_path):
    app, fake, calls = serve(tmp_path, {"meetings": [], "failed": []})
    created = datetime(2026, 9, 8, tzinfo=UTC)
    record, key = create_key(
        "companion", ["notes:read", "sources:read", "sources:write"], created_at=created
    )

    async def keys():
        return ApiKeySet(keys=[record, fake.sources_record, fake.notes_record])

    fake.get_api_keys = keys
    with TestClient(app) as client:
        app.state.store = fake
        app.state.api_key_auth._store = fake
        headers = {"Authorization": f"Bearer {key}"}
        assert (
            client.get(
                "/v1/recordings/waiting", headers={"Authorization": f"Bearer {fake.sources_key}"}
            ).status_code
            == 403
        )
        assert client.get("/v1/recordings/waiting", headers=headers).json() == {
            "meetings": [],
            "failed": [],
        }
        assert (
            client.post(
                "/v1/recordings/abc/retry", headers={"Authorization": f"Bearer {fake.notes_key}"}
            ).status_code
            == 403
        )
        assert client.post("/v1/recordings/abc/retry", headers=headers).status_code == 200
    assert calls == [("recording-waiting", {}), ("retry-failed-recording", {"id": "abc"})]
