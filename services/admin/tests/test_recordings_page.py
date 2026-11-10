"""Admin: set up transcription, watch recordings on the Jobs tab, retry a failed one."""

from coppermind.context_config import ContextConfig, FeatureAssignment, ModelConnection
from services.admin.tests.browser import admin_browser as admin_browser
from services.admin.tests.test_admin import form_token
from services.admin.tests.test_context_page import ContextStore

JOB = {
    "id": "a" * 64,
    "title": "Standup",
    "filename": "standup.m4a",
    "size_bytes": 300 * 1024 * 1024,
    "state": "queued",
    "attempts": 0,
    "attempt_limit": 2,
    "error": "",
    "error_rule": "",
    "retry_after": "",
    "started_at": "",
    "succeeded_at": "",
    "note_id": "",
    "note_path": "",
    "can_retry": False,
}


def store_with(recordings, transcription=None, worker_error=""):
    class Recordings(ContextStore):
        async def context_action(self, action, payload):
            data = await super().context_action(action, payload)
            if action == "snapshot":
                config = ContextConfig(
                    connections=[
                        ModelConnection(
                            id="local",
                            name="Local",
                            provider="compatible",
                            base_url="http://local.example/v1",
                        ),
                        ModelConnection(id="claude", name="Claude", provider="anthropic"),
                    ]
                )
                if transcription is not None:
                    config.assignments["transcription"] = transcription
                data["config"] = config.model_dump()
                data["recordings"] = recordings
                data["recording_worker_error"] = worker_error
            return data

    return Recordings()


def test_the_models_tab_sets_up_transcription_with_plain_words_and_no_anthropic(signed_in):
    client, _ = signed_in
    client.app.state.store = store_with([])
    page = client.get("/admin/context?tab=models")
    assert "Transcription setup" in page.text
    assert "stay stored until you remove them" in page.text
    assert "goes to the connection above and nowhere else" in page.text
    assert "1 GiB" in page.text
    assert "Wait limit (seconds)" in page.text and 'value="3600"' in page.text
    # Only connections that can transcribe are offered; the raw reply format is explained.
    section = page.text.split("Transcription setup")[1]
    assert ">Local</option>" in section and ">Claude</option>" not in section
    assert "A speaker on every segment (diarized_json)" in section
    assert "Transcribe recordings that are handed in" in section


def test_saving_transcription_sends_one_assignment_through_a_guarded_form(signed_in):
    client, _ = signed_in
    fake = store_with([])
    client.app.state.store = fake
    assert client.post("/v1/admin/context/save-transcription", data={}).status_code == 403
    csrf = form_token(client, "/admin/context?tab=models")
    response = client.post(
        "/v1/admin/context/save-transcription",
        data={
            "csrf": csrf,
            "revision": "3",
            "connection": "local",
            "model": "whisper-diarize",
            "response_format": "verbose_json",
            "wait_s": "9000",
            "attempts": "3",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303 and "tab=models" in response.headers["location"]
    action, payload = fake.calls[-1]
    assert action == "save-transcription" and payload["revision"] == 3
    assert payload["assignment"] == {
        "connection": "local",
        "model": "whisper-diarize",
        "response_format": "verbose_json",
        "wait_s": 9000,
        "attempts": 3,
        "enabled": True,
        "paused": False,
    }
    banner = client.get(response.headers["location"])
    assert "Transcription settings saved." in banner.text


def test_a_saved_setup_is_shown_back(signed_in):
    client, _ = signed_in
    saved = FeatureAssignment(
        connection="local",
        model="whisper-diarize",
        enabled=True,
        wait_s=600,
        attempts=3,
        response_format="verbose_json",
    )
    client.app.state.store = store_with([], saved)
    page = client.get("/admin/context?tab=models")
    section = page.text.split("Transcription setup")[1]
    assert 'value="whisper-diarize"' in section and 'value="600"' in section
    assert '<option value="verbose_json" selected>' in section
    assert 'name="enabled" checked' in section


def test_with_no_recordings_the_jobs_tab_says_what_to_do(signed_in):
    client, _ = signed_in
    client.app.state.store = store_with([])
    off = client.get("/admin/context?tab=review").text
    assert "set up transcription" in off and "/admin/context?tab=models#transcription" in off
    saved = FeatureAssignment(connection="local", model="m", enabled=True)
    client.app.state.store = store_with([], saved)
    on = client.get("/admin/context?tab=review").text
    assert "No recordings yet." in on and "lands in Review as a note" in on


def test_each_state_of_a_recording_reads_as_a_sentence(signed_in):
    client, _ = signed_in
    jobs = [
        {**JOB, "id": "1" * 64, "title": "Waiting one"},
        {
            **JOB,
            "id": "2" * 64,
            "title": "Running one",
            "state": "running",
            "attempts": 1,
            "started_at": "2026-11-09T15:00:00+00:00",
        },
        {
            **JOB,
            "id": "3" * 64,
            "title": "Done one",
            "state": "succeeded",
            "note_id": "N1",
            "note_path": "Review/Done.md",
        },
        {
            **JOB,
            "id": "4" * 64,
            "title": "Failed one",
            "state": "failed",
            "attempts": 2,
            "error": "The transcription service answered HTTP 404.",
            "error_rule": "transcription-http-404",
            "can_retry": True,
        },
        {
            **JOB,
            "id": "5" * 64,
            "title": "Backing off",
            "state": "failed",
            "attempts": 1,
            "error": "The transcription service accepted the request but sent nothing.",
            "retry_after": "2099-01-01T12:00:00+00:00",
            "can_retry": True,
        },
    ]
    saved = FeatureAssignment(connection="local", model="m", enabled=True)
    client.app.state.store = store_with(jobs, saved)
    page = client.get("/admin/context?tab=review").text
    assert "standup.m4a, 300.0 MiB" in page
    assert "Waiting for its turn" in page
    assert "Transcribing since" in page and "(attempt 1 of 2)" in page
    assert "Transcript filed. The note is in Review." in page
    assert '<a href="/admin/notes/N1">Open note</a>' in page
    assert "Failed: The transcription service answered HTTP 404. Retry to try again." in page
    assert "The app will try again after" in page
    assert page.count(">Retry</button>") == 2
    assert "A transcript already saved is reused" in page


def test_a_waiting_recording_explains_when_transcription_is_off_or_paused(signed_in):
    client, _ = signed_in
    client.app.state.store = store_with([JOB])
    assert "Transcription is off; turn it on under Models" in (
        client.get("/admin/context?tab=review").text
    )
    paused = FeatureAssignment(connection="local", model="m", enabled=True, paused=True)
    client.app.state.store = store_with([JOB], paused)
    assert "Transcription is paused; resume it under Models" in (
        client.get("/admin/context?tab=review").text
    )


def test_retrying_a_recording_is_a_guarded_form_that_confirms_in_words(signed_in):
    client, _ = signed_in
    failed = {**JOB, "state": "failed", "error": "No.", "can_retry": True}
    fake = store_with([failed], FeatureAssignment(connection="local", model="m", enabled=True))
    client.app.state.store = fake
    assert client.post("/v1/admin/context/retry-recording", data={}).status_code == 403
    csrf = form_token(client, "/admin/context?tab=review")
    response = client.post(
        "/v1/admin/context/retry-recording",
        data={"csrf": csrf, "id": "a" * 64},
        follow_redirects=False,
    )
    assert response.status_code == 303 and "tab=review" in response.headers["location"]
    assert fake.calls[-1] == ("retry-recording", {"id": "a" * 64, "revision": 0})
    page = client.get(response.headers["location"])
    assert "Retry queued for the recording." in page.text


def test_a_worker_fault_is_shown_on_the_jobs_tab(signed_in):
    client, _ = signed_in
    client.app.state.store = store_with([], worker_error="The recording worker failed.")
    assert "The recording worker failed." in client.get("/admin/context?tab=review").text
