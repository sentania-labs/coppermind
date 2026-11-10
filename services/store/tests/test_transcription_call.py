"""The transcription call: a recording streamed out, a slow service waited for, plain failures.

The stand-in below is a real socket server because httpx's mock transport cannot enforce
silence timeouts. It returns the documented response shape (`diarized_json`: segments with a
speaker each) after a delay, optionally sending whitespace while it works.
"""

import asyncio
import json

import pytest
from coppermind_store.model_provider import ModelProvider, ProviderError, parse_transcript

from coppermind.context_config import FeatureAssignment, ModelConnection

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

    def __init__(self, *, delay=0.0, keepalive=0.0, status=200, reply=None):
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
        length = int(headers["content-length"])
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
                await asyncio.sleep(self.delay)
                payload = json.dumps(self.reply).encode()
                writer.write(
                    f"HTTP/1.1 {self.status} X\r\ncontent-type: application/json\r\n"
                    f"content-length: {len(payload)}\r\n\r\n".encode()
                    + payload
                )
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()


def assignment(timeout_s=60, **extra):
    # model_construct skips the 5 s floor so the tests stay fast.
    return FeatureAssignment.model_construct(
        **{**FeatureAssignment().__dict__, "model": "whisper-diarize", "timeout_s": timeout_s}
        | extra
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
        (404, "transcription-http-404", False, "no transcription endpoint"),
        (401, "transcription-http-401", False, "refused the key"),
        (413, "transcription-http-413", False, "too large"),
        (422, "transcription-http-422", False, "model name or the reply format"),
        (503, "transcription-http", True, "healthy"),
        (429, "transcription-http", True, "healthy"),
        (408, "transcription-http", True, "healthy"),
        (409, "transcription-http", False, "request failed"),
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
