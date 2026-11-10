"""Admin: set up transcription, watch recordings, retry a failed one."""

import pytest

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
    assert "Audio is removed after the note is reviewed and filed" in page.text
    assert "goes to the connection above and nowhere else" in page.text
    assert "1 GiB" in page.text
    assert "Wait limit (seconds)" in page.text and 'value="3600"' in page.text
    # Only connections that can transcribe are offered; the raw reply format is explained.
    section = page.text.split("Transcription setup")[1]
    assert ">Local</option>" in section and ">Claude</option>" not in section
    # Plain words on the choices; the protocol names are only in the optional help line.
    assert ">Speaker on every segment</option>" in section
    assert ">Whisper-compatible segments with speaker names</option>" in section
    assert "(diarized_json)" not in section and ">diarized_json<" not in section
    # The protocol names sit inside a collapsed disclosure, not in always-visible help.
    # (The option values are form values, not shown text.)
    section = section.replace('value="diarized_json"', "").replace('value="verbose_json"', "")
    visible = section.split("<details><summary>Technical names</summary>")
    assert len(visible) == 2 and "diarized_json" not in visible[0]
    assert "diarized_json" in visible[1].split("</details>")[0]
    assert "<details open" not in section.split("Technical names")[0][-40:]
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
            "error": "The transcription service could not find the endpoint or the model.",
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
    assert "Transcript ready. Note in Review: Done one" in page
    # No raw-ID diagnostics page: the link goes to the recording's own page.
    assert "/admin/notes/N1" not in page and "Open note" not in page
    assert '<a href="/admin/context/recordings/' + "3" * 64 + '">Read the transcript</a>' in page
    assert "could not find the endpoint or the model." in page and "Try again when" not in page
    assert "HTTP 404" not in page
    # A settings failure links to the settings that can cure it, beside Retry.
    assert 'href="/admin/context?tab=models#transcription">Check the connection and model' in page
    assert "The app will try again after" in page
    assert page.count(">Retry</button>") == 2
    assert "A transcript already saved is reused" in page


def test_a_recording_waiting_on_a_busy_service_says_so_and_offers_no_retry(signed_in):
    client, _ = signed_in
    busy = {
        **JOB,
        "title": "Behind another client",
        "error": "Waiting for the transcription service, which is busy.",
        "error_rule": "transcription-busy",
        "retry_after": "2099-01-01T12:00:00+00:00",
    }
    saved = FeatureAssignment(connection="local", model="m", enabled=True)
    client.app.state.store = store_with([busy], saved)
    page = client.get("/admin/context?tab=review").text
    assert "Waiting for the transcription service, which is busy." in page
    assert "The app asks again after" in page
    assert ">Retry</button>" not in page


def test_a_waiting_recording_explains_when_transcription_is_off_or_paused(signed_in):
    client, _ = signed_in
    client.app.state.store = store_with([JOB])
    off = client.get("/admin/context?tab=review").text
    assert "Transcription is off. Turn it on to start this recording." in off
    assert 'href="/admin/context?tab=models#transcription">Turn on transcription' in off
    assert "under Models" not in off
    paused = FeatureAssignment(connection="local", model="m", enabled=True, paused=True)
    client.app.state.store = store_with([JOB], paused)
    page = client.get("/admin/context?tab=review").text
    assert "Transcription is paused; it starts when you resume it." in page
    assert 'tab=models#transcription">Resume transcription' in page


FAILED = {
    **JOB,
    "state": "failed",
    "attempts": 1,
    "error": "The transcription service reported an error or is busy.",
    "error_rule": "transcription-http",
    "retry_after": "2099-01-01T12:00:00+00:00",
    "can_retry": True,
}


def test_a_failed_recording_while_transcription_is_off_offers_no_retry_and_no_timer(signed_in):
    client, _ = signed_in
    client.app.state.store = store_with([FAILED])
    page = client.get("/admin/context?tab=review").text
    assert "Transcription is off. Turn it on to retry this recording." in page
    assert ">Retry</button>" not in page and "will try again after" not in page
    assert 'href="/admin/context?tab=models#transcription">Turn on transcription' in page


def test_a_failed_recording_while_paused_can_be_queued_and_says_it_starts_on_resume(signed_in):
    client, _ = signed_in
    paused = FeatureAssignment(connection="local", model="m", enabled=True, paused=True)
    client.app.state.store = store_with([FAILED], paused)
    page = client.get("/admin/context?tab=review").text
    assert "a retry will start when you resume it." in page
    assert ">Retry</button>" in page and "will try again after" not in page


def test_an_unexpected_failure_says_the_recording_is_safe_and_never_sends_you_to_a_log(
    signed_in,
):
    message = (
        "Transcription stopped unexpectedly. Your recording is still saved. Try again; if it "
        "fails again, open details."
    )
    failed = {**FAILED, "error": message, "error_rule": "job-error", "retry_after": ""}
    saved = FeatureAssignment(connection="local", model="m", enabled=True)
    client, _ = signed_in
    client.app.state.store = store_with([failed], saved)
    page = client.get("/admin/context?tab=review").text
    assert message in page and "store log" not in page
    assert ">Retry</button>" in page
    assert f'/admin/context/recordings/{"a" * 64}">Details</a>' in page


def test_the_recording_page_shows_the_note_and_transcript_with_ids_only_in_details(signed_in):
    client, _ = signed_in
    done = {
        **JOB,
        "state": "succeeded",
        "transcribed": True,
        "note_id": "01NOTEIDXXXXXXXXXXXXXXXXXX",
        "source_id": "01SOURCEIDXXXXXXXXXXXXXXX",
        "artifact_id": "01ARTIFACTXXXXXXXXXXXXXXX",
        "note_path": "Review/Standup.md",
        "error_rule": "",
        "reply": "",
        "usage": {"elapsed_ms": 5},
        "transcript": "[00:00:00] Speaker 1: Hello <b>there</b>.\n",
        "transcript_cut": False,
    }
    store = store_with([done], FeatureAssignment(connection="local", model="m", enabled=True))
    base = store.context_action

    async def with_detail(action, payload):
        return done if action == "recording-detail" else await base(action, payload)

    store.context_action = with_detail
    client.app.state.store = store
    response = client.get(f"/admin/context/recordings/{'a' * 64}")
    assert response.status_code == 200
    page = response.text
    head, details = page.split("<summary>Technical details</summary>", 1)
    assert "Transcript ready. Note in Review: Standup" in head
    assert "Review/Standup.md" in head and "Speaker 1: Hello &lt;b&gt;there&lt;/b&gt;." in head
    assert "01NOTEIDXXXXXXXXXXXXXXXXXX" not in head and "01SOURCEID" not in head
    assert "01NOTEIDXXXXXXXXXXXXXXXXXX" in details and "Technical details" in page


def test_reviewed_recording_list_shows_current_path_and_removed_audio(signed_in):
    client, _ = signed_in
    done = {
        **JOB,
        "state": "succeeded",
        "note_id": "N1",
        "note_status": "filed",
        "note_path": "Notes/2026/2026-11/Standup.md",
        "audio_removed": True,
    }
    client.app.state.store = store_with([done])
    page = client.get("/admin/context?tab=review").text
    assert "Note reviewed and filed: Standup" in page
    assert "Notes/2026/2026-11/Standup.md" in page
    assert "Audio removed after review" in page
    assert "transcript remains available" in page
    assert "hash, size, length and archive location" in page
    assert "Note in Review" not in page


def test_reviewed_recording_detail_shows_current_path_and_removed_audio(signed_in):
    client, _ = signed_in
    done = {
        **JOB,
        "state": "succeeded",
        "note_id": "N1",
        "note_status": "filed",
        "note_path": "Notes/2026/2026-11/Standup.md",
        "audio_removed": True,
        "transcribed": True,
        "source_id": "S1",
        "artifact_id": "A1",
        "reply": "",
        "usage": {},
        "transcript": "Speaker 1: Hello.",
        "transcript_cut": False,
    }
    store = store_with([done])
    base = store.context_action

    async def with_detail(action, payload):
        return done if action == "recording-detail" else await base(action, payload)

    store.context_action = with_detail
    client.app.state.store = store
    page = client.get(f"/admin/context/recordings/{'a' * 64}").text
    assert "Note reviewed and filed: Standup" in page
    assert "Notes/2026/2026-11/Standup.md" in page
    assert "Audio removed after review" in page
    assert "hash, size, length and archive location" in page
    assert "Speaker 1: Hello." in page
    assert "Note in Review" not in page and "stays unreviewed" not in page


def test_recording_with_unavailable_note_does_not_claim_it_needs_review(signed_in):
    client, _ = signed_in
    done = {
        **JOB,
        "state": "succeeded",
        "note_id": "N1",
        "note_status": "unavailable",
        "note_path": "",
        "audio_removed": True,
        "transcribed": True,
        "source_id": "S1",
        "artifact_id": "A1",
        "reply": "",
        "usage": {},
        "transcript": "Speaker 1: Hello.",
        "transcript_cut": False,
    }
    store = store_with([done])
    base = store.context_action

    async def with_detail(action, payload):
        return done if action == "recording-detail" else await base(action, payload)

    store.context_action = with_detail
    client.app.state.store = store
    page = client.get(f"/admin/context/recordings/{'a' * 64}").text
    assert "current location is unavailable" in page
    assert "stays unreviewed" not in page and "Note in Review" not in page


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


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        (
            "transcription-audio_unreadable",
            "The recording could not be read. Send a readable audio file again",
        ),
        ("transcription-no_speech", "No speech was found. Send a corrected recording again"),
    ],
)
@pytest.mark.parametrize("enabled", [True, False])
def test_audio_needing_replacement_has_no_retry(signed_in, rule, message, enabled):
    client, _ = signed_in
    failed = {
        **JOB,
        "state": "failed",
        "error_rule": rule,
        "error": "Old message",
        "can_retry": True,
    }
    saved = FeatureAssignment(connection="local", model="m", enabled=enabled)
    client.app.state.store = store_with([failed], saved)
    page = client.get("/admin/context?tab=review").text
    assert message in page
    assert "the same way recordings normally arrive" in page
    assert ">Retry</button>" not in page
    assert "Turn it on to retry" not in page


@pytest.mark.parametrize("rule", ["transcription-unfinished", "transcription-internal"])
def test_transient_transcription_failure_keeps_retry(signed_in, rule):
    client, _ = signed_in
    failed = {
        **JOB,
        "state": "failed",
        "error_rule": rule,
        "error": "Service problem",
        "can_retry": True,
    }
    saved = FeatureAssignment(connection="local", model="m", enabled=True)
    client.app.state.store = store_with([failed], saved)
    page = client.get("/admin/context?tab=review").text
    assert ">Retry</button>" in page


def test_recovering_recording_shows_recovery_message_with_note_status(signed_in):
    client, _ = signed_in
    recovering = {
        **JOB,
        "state": "running",
        "note_status": "unreviewed",
        "error_rule": "transcription-recovering",
        "error": "The transcription service lost the job; the recording was sent again.",
    }
    client.app.state.store = store_with(
        [recovering], FeatureAssignment(connection="local", model="m", enabled=True)
    )
    page = client.get("/admin/context?tab=review").text
    assert "The transcription service lost the job; the recording was sent again." in page
    assert ">Retry</button>" not in page
