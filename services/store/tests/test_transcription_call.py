"""The transcription call: a recording streamed out, a slow service waited for, plain failures.

The stand-in below is a real socket server because httpx's mock transport cannot enforce
silence timeouts. It returns the documented response shape (`diarized_json`: segments with a
speaker each) after a delay, optionally sending whitespace while it works.
"""

import asyncio
import json

import httpx
import pytest
from coppermind_store.model_provider import ModelProvider, ProviderError, parse_transcript

from coppermind.context_config import MAX_TRANSCRIBE_SECONDS, FeatureAssignment, ModelConnection

REPLY = {
    "text": "Hello there. Hi.",
    "language": "en",
    "duration": 7.5,
    "usage": {"seconds": 8},
    "segments": [
        {"id": "seg_1", "start": 0.0, "end": 3.2, "text": " Hello there.", "speaker": "Speaker 1"},
        {"id": "seg_2", "start": 3.4, "end": 7.5, "text": "Hi.", "speaker": "Speaker 2"},
    ],
}


class Service:
    """An OpenAI-compatible `POST /v1/audio/transcriptions` that takes its time."""

    def __init__(
        self,
        *,
        delay=0.0,
        keepalive=0.0,
        status=200,
        reply=None,
        jobs=False,
        job_status="done",
        retry_after=None,
        poll_delay=0.0,
    ):
        self.poll_delay = poll_delay
        self.jobs = jobs
        self.job_status, self.retry_after = job_status, retry_after
        self.delay, self.keepalive, self.status = delay, keepalive, status
        self.reply = REPLY if reply is None else reply
        self.requests: list[dict] = []

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    @property
    def connection(self):
        return ModelConnection(
            id="stt",
            name="Speech",
            provider="compatible",
            base_url=f"http://127.0.0.1:{self.port}/v1",
        )

    async def handle(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        headers = {
            line.split(b":", 1)[0].lower().decode(): line.split(b":", 1)[1].strip().decode()
            for line in lines[1:]
            if b":" in line
        }
        length = int(headers.get("content-length", "0"))
        body = b""
        while len(body) < length:
            piece = await reader.read(min(65536, length - len(body)))
            if not piece:
                break
            body += piece
        self.requests.append({"path": lines[0].decode(), "headers": headers, "body": body})
        try:
            if self.keepalive:
                writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n")
                writer.write(b"transfer-encoding: chunked\r\n\r\n")
                waited = 0.0
                while waited < self.delay:
                    await asyncio.sleep(self.keepalive)
                    waited += self.keepalive
                    writer.write(b"1\r\n \r\n")
                    await writer.drain()
                payload = json.dumps(self.reply).encode()
                writer.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n")
            else:
                await asyncio.sleep(self.poll_delay if lines[0].startswith(b"GET ") else self.delay)
                answer = self.reply
                if self.jobs and self.status == 200:
                    if lines[0].startswith(b"POST "):
                        answer = {"id": "local-job", "status": "queued"}
                    else:
                        answer = (
                            {"status": "done", "result": self.reply}
                            if self.job_status == "done"
                            else {"status": self.job_status}
                            if self.job_status
                            else {}
                        )
                payload = json.dumps(answer).encode()
                extra = f"retry-after: {self.retry_after}\r\n" if self.retry_after else ""
                writer.write(
                    f"HTTP/1.1 {self.status} X\r\ncontent-type: application/json\r\n{extra}"
                    f"content-length: {len(payload)}\r\n\r\n".encode()
                    + payload
                )
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()


def assignment(wait_s=60, **extra):
    # model_construct skips the 5 s floor so the tests stay fast.
    return FeatureAssignment.model_construct(
        **{**FeatureAssignment().__dict__, "model": "whisper-diarize", "wait_s": wait_s} | extra
    )


@pytest.fixture
def recording(tmp_path):
    path = tmp_path / "meeting.m4a"
    # Larger than one upload chunk, so the file is sent in several pieces.
    path.write_bytes(bytes(range(256)) * (3 * 4096 + 17))
    return path


async def send(service, recording, wait=60, **extra):
    return await ModelProvider().transcribe(
        service.connection,
        "",
        assignment(wait, **extra),
        recording,
        filename=recording.name,
        mime_type="audio/mp4",
        size_bytes=recording.stat().st_size,
    )


async def test_the_recording_is_sent_whole_as_a_multipart_upload_and_segments_come_back(
    recording,
):
    async with Service() as service:
        transcript, usage = await send(service, recording)
    request = service.requests[0]
    assert request["path"].startswith("POST /v1/audio/transcriptions ")
    assert request["headers"]["content-type"].startswith("multipart/form-data; boundary=")
    body = request["body"]
    assert recording.read_bytes() in body
    assert b'name="num_speakers"' not in body
    assert b'name="model"\r\n\r\nwhisper-diarize' in body
    assert b'name="response_format"\r\n\r\ndiarized_json' in body
    assert b'name="chunking_strategy"\r\n\r\nauto' in body
    assert b'filename="meeting.m4a"' in body and b"Content-Type: audio/mp4" in body
    # Speakers are kept exactly as the service labelled them.
    assert [s["speaker"] for s in transcript["segments"]] == ["Speaker 1", "Speaker 2"]
    assert transcript["segments"][0]["text"] == "Hello there."
    assert transcript["language"] == "en" and transcript["duration"] == 7.5
    assert usage["seconds"] == 8 and usage["model"] == "whisper-diarize"


async def test_verbose_json_is_asked_for_without_a_chunking_strategy(recording):
    async with Service() as service:
        await send(service, recording, response_format="verbose_json")
    assert b'name="response_format"\r\n\r\nverbose_json' in service.requests[0]["body"]
    assert b"chunking_strategy" not in service.requests[0]["body"]


async def test_a_service_that_works_quietly_for_longer_than_the_wait_limit_is_waited_out(
    recording,
):
    # Silent but for a keep-alive byte: never quiet for the 2 s limit, yet slower overall.
    async with Service(delay=3.0, keepalive=0.5) as service:
        transcript, usage = await send(service, recording, wait=2)
    assert len(transcript["segments"]) == 2 and usage["elapsed_ms"] >= 3000


async def test_silence_for_the_whole_wait_limit_is_a_waiting_error_that_names_the_service(
    recording,
):
    async with Service(delay=3.0) as service:
        with pytest.raises(ProviderError) as caught:
            await send(service, recording, wait=1)
    assert caught.value.rule == "provider-no-response" and caught.value.waiting
    message = str(caught.value)
    assert "transcription service" in message and "wait limit 1 s" in message
    assert "under Transcription" in message


async def test_a_refused_connection_does_not_wait_and_names_the_address(recording):
    service = Service()
    async with service:
        connection = service.connection
    with pytest.raises(ProviderError) as caught:
        await ModelProvider().transcribe(
            connection,
            "",
            assignment(5),
            recording,
            filename="a.m4a",
            mime_type="audio/mp4",
            size_bytes=recording.stat().st_size,
        )
    assert caught.value.rule == "provider-connect"
    assert "transcription service" in str(caught.value)


@pytest.mark.parametrize(
    ("status", "rule", "waiting", "phrase"),
    [
        (404, "transcription-http-404", False, "could not find the endpoint"),
        (401, "transcription-http-401", False, "refused the key"),
        (413, "transcription-http-413", False, "too large"),
        (422, "transcription-http-422", False, "model name or the reply format"),
        (503, "transcription-http", True, "healthy"),
        (409, "transcription-http-409", False, "refused the request"),
        (429, "transcription-busy", True, "busy"),
        (408, "transcription-http", True, "healthy"),
    ],
)
async def test_an_http_failure_is_told_apart_and_only_a_server_fault_is_tried_again(
    recording, status, rule, waiting, phrase
):
    async with Service(status=status, reply={"error": {"message": "secret detail"}}) as service:
        with pytest.raises(ProviderError) as caught:
            await send(service, recording)
    assert caught.value.rule == rule and caught.value.waiting is waiting
    assert phrase in str(caught.value) and "secret detail" not in str(caught.value)


async def test_an_unusable_answer_is_a_refusal_the_same_upload_would_only_repeat(recording):
    from coppermind_store.recordings import repeats_only

    async with Service(reply=["not", "an", "object"]) as service:
        with pytest.raises(ProviderError) as caught:
            await send(service, recording)
    assert caught.value.rule == "transcription-provider" and caught.value.waiting is False
    assert "answer could not be used" in str(caught.value)
    assert repeats_only(caught.value.rule)


async def test_a_keyless_openai_connection_is_refused_as_a_connection_problem(recording):
    from coppermind_store.recordings import repeats_only

    connection = ModelConnection(id="o", name="O", provider="openai")
    with pytest.raises(ProviderError) as caught:
        await ModelProvider().transcribe(
            connection,
            "",
            assignment(),
            recording,
            filename="a.m4a",
            mime_type="audio/mp4",
            size_bytes=recording.stat().st_size,
        )
    assert "requires an API key" in str(caught.value) and repeats_only(caught.value.rule)


async def test_anthropic_is_refused_without_a_call(recording):
    connection = ModelConnection(id="a", name="A", provider="anthropic")
    with pytest.raises(ProviderError) as caught:
        await ModelProvider().transcribe(
            connection,
            "key",
            assignment(),
            recording,
            filename="a.m4a",
            mime_type="audio/mp4",
            size_bytes=1,
        )
    assert caught.value.rule == "transcription-provider"


async def test_a_recording_that_changes_while_it_is_sent_is_not_accepted(recording):
    async with Service() as service:
        with pytest.raises(ProviderError) as caught:
            await ModelProvider().transcribe(
                service.connection,
                "",
                assignment(),
                recording,
                filename="a.m4a",
                mime_type="audio/mp4",
                size_bytes=recording.stat().st_size + 5,
            )
    assert caught.value.rule in {"recording-changed", "provider-dropped"}


def test_a_reply_without_speakers_is_refused_with_a_reason_for_each_case():
    flat = {"text": "hello", "segments": [{"text": "hello", "start": 0, "end": 1}]}
    with pytest.raises(ProviderError) as caught:
        parse_transcript(flat)
    assert caught.value.rule == "transcript-no-speakers"
    with pytest.raises(ProviderError) as caught:
        parse_transcript({"text": "hello"})
    assert caught.value.rule == "transcript-flat"
    with pytest.raises(ProviderError) as caught:
        parse_transcript({"text": "  ", "segments": [{"text": " ", "speaker": "A"}]})
    assert caught.value.rule == "transcript-empty"


def test_speaker_labels_are_kept_as_given_even_when_numeric_or_missing_on_a_segment():
    parsed = parse_transcript(
        {
            "segments": [
                {"text": "a", "speaker": 0, "start": 1},
                {"text": "b", "speaker": "A"},
                {"text": "c"},
            ]
        }
    )
    assert [s["speaker"] for s in parsed["segments"]] == ["0", "A", ""]
    assert parsed["segments"][1]["start"] is None


class VirtualClock:
    """Moves the running loop's clock forward without waiting, so hours pass in no time."""

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.offset = 0.0
        real = self.loop.time
        self.loop.time = lambda: real() + self.offset  # type: ignore[method-assign]

    def advance(self, seconds):
        self.offset += seconds


def mock_service(clock, took, seen):
    async def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        await request.aread()
        clock.advance(took)
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=REPLY)

    return httpx.MockTransport(handle)


async def call(transport, recording, wait_s):
    connection = ModelConnection(
        id="stt", name="Speech", provider="compatible", base_url="http://stt.lab/v1"
    )
    return await ModelProvider(transport).transcribe(
        connection,
        "",
        assignment(wait_s),
        recording,
        filename="meeting.m4a",
        mime_type="audio/mp4",
        size_bytes=recording.stat().st_size,
    )


def test_the_transcription_wait_limit_reaches_the_four_hour_ceiling_the_enrichment_one_cannot():
    assert FeatureAssignment().wait_s == 3600
    assert FeatureAssignment(wait_s=MAX_TRANSCRIBE_SECONDS).wait_s == 4 * 3600
    with pytest.raises(ValueError):
        FeatureAssignment(wait_s=MAX_TRANSCRIBE_SECONDS + 1)
    with pytest.raises(ValueError):
        FeatureAssignment(timeout_s=1801)


async def test_a_service_that_answers_after_more_than_the_old_half_hour_bound_is_waited_for(
    recording,
):
    clock = VirtualClock()
    seen: list[dict] = []
    transcript, _ = await call(mock_service(clock, 2 * 3600, seen), recording, 3 * 3600)
    assert len(transcript["segments"]) == 2
    # The connection waits the whole limit for the answer but not for the connection to open.
    assert seen[0]["read"] == 3 * 3600 and seen[0]["connect"] == 60


async def test_a_service_still_working_past_the_ceiling_is_given_up_on_and_not_retried_by_itself(
    recording,
):
    clock = VirtualClock()
    seen: list[dict] = []
    with pytest.raises(ProviderError) as caught:
        await call(mock_service(clock, MAX_TRANSCRIBE_SECONDS + 60, seen), recording, 3600)
    assert caught.value.rule == "provider-too-long"
    assert "Split the recording" in str(caught.value)


@pytest.mark.parametrize(
    ("status", "tried_again"),
    [(409, False), (404, False), (451, False), (429, True), (408, True), (503, True)],
)
async def test_only_a_failure_that_can_pass_is_sent_again_by_itself(recording, status, tried_again):
    from coppermind_store.recordings import repeats_only

    async with Service(status=status) as service:
        with pytest.raises(ProviderError) as caught:
            await send(service, recording)
    assert repeats_only(caught.value.rule) is not tried_again


async def test_job_path_polls_and_submits_stable_identity_and_hint(recording, monkeypatch):
    from coppermind_store import model_provider

    monkeypatch.setattr(model_provider, "JOB_POLL_SECONDS", 0)
    requests = []

    async def handle(request):
        requests.append(
            (
                request.method,
                request.url.path,
                await request.aread(),
                request.headers.get("Idempotency-Key"),
            )
        )
        if request.method == "POST":
            return httpx.Response(200, json={"id": "job-1", "status": "queued"})
        if len(requests) == 2:
            return httpx.Response(200, json={"status": "running"})
        return httpx.Response(200, json={"status": "done", "result": REPLY})

    provider = ModelProvider(httpx.MockTransport(handle))
    connection = ModelConnection(
        id="stt", name="Speech", provider="compatible", base_url="http://127.0.0.1:1/v1"
    )
    result, _ = await provider.transcribe(
        connection,
        "",
        assignment(),
        recording,
        filename=recording.name,
        mime_type="audio/mp4",
        size_bytes=recording.stat().st_size,
        job_id="stable-id",
        duration_seconds=7201,
        num_speakers=3,
    )
    assert result["segments"]
    assert requests[0][1] == "/v1/audio/transcriptions/jobs"
    assert requests[0][3] == "stable-id"
    assert b'name="num_speakers"\r\n\r\n3' in requests[0][2]
    assert requests[1][1] == "/v1/audio/transcriptions/jobs/job-1"
    assert [r[0] for r in requests] == ["POST", "GET", "GET"]


@pytest.mark.parametrize(
    "reason", ["busy", "internal", "audio_unreadable", "no_speech", "too_long"]
)
async def test_service_reason_codes_override_http_status(recording, reason):
    async def handle(request):
        return httpx.Response(400, json={"reason": reason})

    provider = ModelProvider(httpx.MockTransport(handle))
    connection = ModelConnection(
        id="stt", name="Speech", provider="compatible", base_url="http://127.0.0.1:1/v1"
    )
    with pytest.raises(ProviderError) as error:
        await provider.transcribe(
            connection,
            "",
            assignment(),
            recording,
            filename=recording.name,
            mime_type="audio/mp4",
            size_bytes=recording.stat().st_size,
        )
    assert error.value.rule == "transcription-" + reason
    assert error.value.waiting == (reason in {"busy", "internal"})


async def test_direct_too_long_falls_back_to_job_path(recording, monkeypatch):
    from coppermind_store import model_provider

    monkeypatch.setattr(model_provider, "JOB_POLL_SECONDS", 0)
    paths = []

    async def handle(request):
        paths.append(request.url.path)
        if len(paths) == 1:
            return httpx.Response(413, json={"reason": "too_long"})
        return httpx.Response(200, json={"id": "job-1", "status": "done", "result": REPLY})

    provider = ModelProvider(httpx.MockTransport(handle))
    connection = ModelConnection(
        id="stt", name="Speech", provider="compatible", base_url="http://127.0.0.1:1/v1"
    )
    await provider.transcribe(
        connection,
        "",
        assignment(),
        recording,
        filename=recording.name,
        mime_type="audio/mp4",
        size_bytes=recording.stat().st_size,
        job_id="stable-id",
    )
    assert paths == ["/v1/audio/transcriptions", "/v1/audio/transcriptions/jobs"]


async def test_long_recording_against_local_job_service(recording, monkeypatch):
    from coppermind_store import model_provider

    monkeypatch.setattr(model_provider, "JOB_POLL_SECONDS", 0)
    async with Service(jobs=True) as service:
        transcript, _ = await ModelProvider().transcribe(
            service.connection,
            "",
            assignment(),
            recording,
            filename=recording.name,
            mime_type="audio/mp4",
            size_bytes=recording.stat().st_size,
            job_id="stable",
            duration_seconds=7201,
        )
    assert transcript["segments"]
    assert service.requests[0]["path"].startswith("POST /v1/audio/transcriptions/jobs ")
    assert service.requests[1]["path"].startswith("GET /v1/audio/transcriptions/jobs/local-job ")


def test_reason_codes_choose_retry_policy():
    from coppermind_store.recordings import repeats_only

    for reason in ("audio_unreadable", "no_speech", "too_long"):
        assert repeats_only("transcription-" + reason)
    for reason in ("busy", "internal"):
        assert not repeats_only("transcription-" + reason)


def test_a_job_is_given_one_and_a_half_times_the_audio_never_under_an_hour_else_eight_hours():
    from coppermind_store.model_provider import job_budget_seconds

    assert job_budget_seconds(4 * 3600) == 6 * 3600
    assert job_budget_seconds(600) == 3600
    assert job_budget_seconds(None) == 8 * 3600


@pytest.mark.parametrize(
    ("header", "asked"),
    [("1", 60), ("300", 300), ("99999", 900), ("0", None), ("soon", None), (None, None)],
)
async def test_a_busy_answer_is_a_wait_that_carries_the_retry_after_kept_within_bounds(
    recording, header, asked
):
    from coppermind_store.recordings import repeats_only

    async with Service(status=429, retry_after=header) as service:
        with pytest.raises(ProviderError) as caught:
            await send(service, recording)
    assert caught.value.rule == "transcription-busy"
    assert caught.value.retry_after == asked
    assert not repeats_only(caught.value.rule)


@pytest.mark.parametrize("job_status", ["running", "queued", None])
async def test_a_job_that_never_finishes_ends_instead_of_holding_the_worker(
    recording, monkeypatch, job_status
):
    from coppermind_store import model_provider

    monkeypatch.setattr(model_provider, "JOB_POLL_SECONDS", 0.01)
    monkeypatch.setattr(model_provider, "JOB_PACE_HEADROOM", 0)
    monkeypatch.setattr(model_provider, "JOB_MIN_SECONDS", 0.3)
    async with Service(jobs=True, job_status=job_status) as service:
        with pytest.raises(ProviderError) as caught:
            await asyncio.wait_for(
                ModelProvider().transcribe(
                    service.connection,
                    "",
                    assignment(),
                    recording,
                    filename=recording.name,
                    mime_type="audio/mp4",
                    size_bytes=recording.stat().st_size,
                    job_id="stable",
                    duration_seconds=7201,
                ),
                30,
            )
    assert caught.value.rule == "transcription-unfinished"
    assert "did not finish this recording" in str(caught.value)


async def test_an_empty_answer_repeated_ends_the_wait_before_the_deadline(recording, monkeypatch):
    from coppermind_store import model_provider

    monkeypatch.setattr(model_provider, "JOB_POLL_SECONDS", 0)
    async with Service(jobs=True, job_status=None) as service:
        with pytest.raises(ProviderError) as caught:
            await ModelProvider().transcribe(
                service.connection,
                "",
                assignment(),
                recording,
                filename=recording.name,
                mime_type="audio/mp4",
                size_bytes=recording.stat().st_size,
                job_id="stable",
                duration_seconds=7201,
            )
    assert caught.value.rule == "transcription-unfinished"
    assert len(service.requests) == 1 + model_provider.JOB_EMPTY_ANSWERS


@pytest.mark.parametrize(
    ("duration", "path"),
    [
        (None, "/v1/audio/transcriptions"),
        (600, "/v1/audio/transcriptions"),
        (7200, "/v1/audio/transcriptions"),
        (7201, "/v1/audio/transcriptions/jobs"),
    ],
)
async def test_only_a_recording_known_to_be_over_two_hours_starts_on_the_jobs_path(
    recording, duration, path
):
    paths = []

    async def handle(request):
        paths.append(request.url.path)
        await request.aread()
        return httpx.Response(200, json={"id": "j", "status": "done", "result": REPLY, **REPLY})

    connection = ModelConnection(
        id="stt", name="Speech", provider="compatible", base_url="http://127.0.0.1:1/v1"
    )
    await ModelProvider(httpx.MockTransport(handle)).transcribe(
        connection,
        "",
        assignment(),
        recording,
        filename=recording.name,
        mime_type="audio/mp4",
        size_bytes=recording.stat().st_size,
        job_id="stable-id",
        duration_seconds=duration,
    )
    assert paths == [path]


async def test_stalled_poll_cannot_overrun_job_deadline(recording, monkeypatch):
    from coppermind_store import model_provider

    monkeypatch.setattr(model_provider, "JOB_POLL_SECONDS", 0)
    monkeypatch.setattr(model_provider, "JOB_PACE_HEADROOM", 0)
    monkeypatch.setattr(model_provider, "JOB_MIN_SECONDS", 0.2)
    async with Service(jobs=True, poll_delay=2) as service:
        with pytest.raises(ProviderError) as caught:
            await asyncio.wait_for(
                ModelProvider().transcribe(
                    service.connection,
                    "",
                    assignment(),
                    recording,
                    filename=recording.name,
                    mime_type="audio/mp4",
                    size_bytes=recording.stat().st_size,
                    job_id="stable",
                    duration_seconds=7201,
                ),
                timeout=1,
            )
    assert caught.value.rule == "transcription-unfinished"
    assert len(service.requests) == 2


@pytest.mark.parametrize(
    "failure", ["lost", "purged", "internal", "unknown", "outage", "disconnected"]
)
async def test_job_recovers_with_same_key(recording, monkeypatch, failure):
    from coppermind_store import model_provider

    monkeypatch.setattr(model_provider, "JOB_POLL_SECONDS", 0)
    monkeypatch.setattr(model_provider, "MIN_RETRY_AFTER_SECONDS", 0)
    requests = []
    updates = []

    async def handle(request):
        requests.append((request.method, request.headers.get("Idempotency-Key")))
        if request.method == "POST":
            return httpx.Response(202, json={"id": f"job-{len(requests)}", "status": "queued"})
        if len(requests) == 2:
            if failure in {"lost", "purged"}:
                return httpx.Response(404, json={"error": {"reason": "not_found"}})
            if failure == "disconnected":
                raise httpx.ConnectError("unavailable", request=request)
            if failure == "outage":
                return httpx.Response(503, json={"error": {"reason": "internal"}})
            return httpx.Response(200, json={"status": "failed", "error": {"reason": failure}})
        return httpx.Response(200, json={"status": "done", "result": REPLY})

    provider = ModelProvider(httpx.MockTransport(handle))
    connection = ModelConnection(
        id="stt", name="Speech", provider="compatible", base_url="http://127.0.0.1:1/v1"
    )
    result, _ = await provider.transcribe(
        connection,
        "",
        assignment(),
        recording,
        filename=recording.name,
        mime_type="audio/mp4",
        size_bytes=recording.stat().st_size,
        job_id="stable-id",
        duration_seconds=7201,
        job_update=lambda *args: updates.append(args),
    )
    assert result["segments"]
    posts = [key for method, key in requests if method == "POST"]
    assert posts == ["stable-id"] * (1 if failure in {"outage", "disconnected"} else 2)
    assert any(update[3] for update in updates)
    assert updates[-1][3] == ""


async def test_a_saved_job_older_than_its_budget_is_still_polled_once_after_a_restart(
    recording, monkeypatch
):
    import time

    from coppermind_store import model_provider

    monkeypatch.setattr(model_provider, "JOB_POLL_SECONDS", 0)

    requests = []

    async def handle(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json={"status": "done", "result": REPLY})

    connection = ModelConnection(
        id="stt", name="Speech", provider="compatible", base_url="http://127.0.0.1:1/v1"
    )
    result, _ = await ModelProvider(httpx.MockTransport(handle)).transcribe(
        connection,
        "",
        assignment(),
        recording,
        filename=recording.name,
        mime_type="audio/mp4",
        size_bytes=recording.stat().st_size,
        job_id="stable",
        duration_seconds=2400,
        remote_job_id="saved-job",
        job_started=time.time() - 3 * 3600,
    )
    assert result["segments"]
    assert requests == [("GET", "/v1/audio/transcriptions/jobs/saved-job")]


@pytest.mark.parametrize("reason", ["audio_unreadable", "no_speech", "too_long"])
async def test_failed_job_is_not_resubmitted_for_bad_audio(recording, monkeypatch, reason):
    from coppermind_store import model_provider

    monkeypatch.setattr(model_provider, "JOB_POLL_SECONDS", 0)
    requests = []

    async def handle(request):
        requests.append(request.method)
        if request.method == "POST":
            return httpx.Response(202, json={"id": "job", "status": "queued"})
        return httpx.Response(200, json={"status": "failed", "error": {"reason": reason}})

    connection = ModelConnection(
        id="stt", name="Speech", provider="compatible", base_url="http://127.0.0.1:1/v1"
    )
    with pytest.raises(ProviderError) as exc:
        await ModelProvider(httpx.MockTransport(handle)).transcribe(
            connection,
            "",
            assignment(),
            recording,
            filename=recording.name,
            mime_type="audio/mp4",
            size_bytes=recording.stat().st_size,
            job_id="stable",
            duration_seconds=7201,
        )
    assert exc.value.rule == "transcription-" + reason
    assert requests == ["POST", "GET"]


async def test_repeated_loss_hits_cap_and_restart_resumes_poll(recording, monkeypatch):
    from coppermind_store import model_provider

    monkeypatch.setattr(model_provider, "JOB_POLL_SECONDS", 0)
    requests = []
    updates = []

    async def handle(request):
        requests.append((request.method, request.url.path, request.headers.get("Idempotency-Key")))
        if request.method == "GET":
            return httpx.Response(404, json={"error": {"reason": "not_found"}})
        return httpx.Response(202, json={"id": "new-job", "status": "queued"})

    connection = ModelConnection(
        id="stt", name="Speech", provider="compatible", base_url="http://127.0.0.1:1/v1"
    )
    with pytest.raises(ProviderError) as exc:
        await ModelProvider(httpx.MockTransport(handle)).transcribe(
            connection,
            "",
            assignment(),
            recording,
            filename=recording.name,
            mime_type="audio/mp4",
            size_bytes=recording.stat().st_size,
            job_id="stable",
            duration_seconds=7201,
            remote_job_id="prior-job",
            job_update=lambda *args: updates.append(args),
        )
    assert exc.value.rule == "transcription-unfinished"
    assert "kept losing or failing" in str(exc.value)
    assert requests[0][1].endswith("/prior-job")
    assert [key for method, _, key in requests if method == "POST"] == [
        "stable"
    ] * model_provider.JOB_MAX_RESUBMITS
    assert updates[-1][2] == model_provider.JOB_MAX_RESUBMITS


async def test_expired_saved_job_is_polled_without_waiting_for_poll_interval(
    recording, monkeypatch
):
    import time

    from coppermind_store import model_provider

    monkeypatch.setattr(model_provider, "JOB_POLL_SECONDS", 1)
    requests = []

    async def handle(request):
        requests.append(request.method)
        return httpx.Response(200, json={"status": "done", "result": REPLY})

    connection = ModelConnection(
        id="stt", name="Speech", provider="compatible", base_url="http://127.0.0.1:1/v1"
    )
    result, _ = await ModelProvider(httpx.MockTransport(handle)).transcribe(
        connection,
        "",
        assignment(wait_s=0.1),
        recording,
        filename=recording.name,
        mime_type="audio/mp4",
        size_bytes=recording.stat().st_size,
        job_id="stable",
        duration_seconds=7201,
        remote_job_id="saved",
        job_started=time.time() - 86400,
    )
    assert result["segments"]
    assert requests == ["GET"]


@pytest.mark.parametrize("failure", ["lost", "internal"])
async def test_recovery_has_time_to_resubmit_when_old_deadline_is_near(
    recording, monkeypatch, failure
):
    from coppermind_store import model_provider

    monkeypatch.setattr(model_provider, "JOB_POLL_SECONDS", 0)
    monkeypatch.setattr(model_provider, "JOB_MIN_SECONDS", 0.2)
    monkeypatch.setattr(model_provider, "JOB_PACE_HEADROOM", 0)
    monkeypatch.setattr(model_provider, "MIN_RETRY_AFTER_SECONDS", 0.12)
    requests = []

    async def handle(request):
        requests.append((request.method, request.headers.get("Idempotency-Key")))
        if len(requests) == 1:
            return httpx.Response(202, json={"id": "first", "status": "queued"})
        if len(requests) == 2:
            await asyncio.sleep(0.12)
            if failure == "lost":
                return httpx.Response(404, json={"error": {"reason": "not_found"}})
            return httpx.Response(200, json={"status": "failed", "error": {"reason": "internal"}})
        if request.method == "POST":
            if failure == "lost":
                await asyncio.sleep(0.12)
            return httpx.Response(
                202, json={"id": "replacement", "status": "done", "result": REPLY}
            )
        raise AssertionError("unexpected request")

    connection = ModelConnection(
        id="stt", name="Speech", provider="compatible", base_url="http://127.0.0.1:1/v1"
    )
    result, _ = await ModelProvider(httpx.MockTransport(handle)).transcribe(
        connection,
        "",
        assignment(),
        recording,
        filename=recording.name,
        mime_type="audio/mp4",
        size_bytes=recording.stat().st_size,
        job_id="stable",
        duration_seconds=7201,
    )
    assert result["segments"]
    assert requests == [("POST", "stable"), ("GET", None), ("POST", "stable")]
