"""Bounded provider calls. Remote errors never expose headers or response bodies."""

from __future__ import annotations

import asyncio
import json
import math
import secrets
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from coppermind.context_config import FeatureAssignment, ModelConnection

# The longest the app waits for one model call, however steadily the reply arrives. The
# per-job wait limit is a silence limit; this stops a trickle from holding the single
# worker forever.
MAX_CALL_SECONDS = 3600
# The most a streamed reply may send in total, framing included. Replies are held to
# 2 MiB of text; this is the wire-size ceiling above that.
MAX_STREAM_BYTES = 16 * 1024 * 1024
# The longest vector accepted. pgvector stores up to 16,000 dimensions.
MAX_EMBEDDING_DIMENSIONS = 8192
# How much of a model's reply is kept on a failed job so Inspect can show why it failed.
REPLY_KEPT_CHARS = 4000
# The most one recording may take to transcribe, however steadily the service answers. An
# hour of audio can take 10 to 20 minutes on a CPU; this leaves room for several hours.
MAX_TRANSCRIBE_SECONDS = 4 * 3600
# The largest transcript reply read: speaker segments with timings run to a few MiB for hours.
MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1 << 20


class ProviderError(ValueError):
    """A provider call failed. `rule` is a stable name; `waiting` marks a timeout, where the
    right response is to wait before trying again rather than to try again at once."""

    def __init__(
        self, message: str, rule: str = "", *, reply: str = "", waiting: bool = False
    ) -> None:
        super().__init__(message)
        self.rule = rule
        self.reply = reply[:REPLY_KEPT_CHARS]
        self.waiting = waiting


class InputLimitError(ProviderError):
    """The note and transcript text is larger than the configured limit."""

    def __init__(self, size: int, limit: int, *, at_least: bool = False) -> None:
        super().__init__("input exceeds the configured limit; nothing was sent")
        self.size = size
        self.limit = limit
        # True when `size` is a proven lower bound, not the complete count.
        self.at_least = at_least


class CallStats:
    """What one call looked like from the client, for the Admin speed readout."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.first_byte: float | None = None
        self.streamed = False

    def mark_first_byte(self) -> None:
        if self.first_byte is None:
            self.first_byte = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def usage(self) -> dict[str, Any]:
        first = self.first_byte if self.first_byte is not None else time.monotonic()
        return {
            "elapsed_ms": int(self.elapsed * 1000),
            "first_byte_ms": int((first - self.started) * 1000),
            "streamed": self.streamed,
        }


def _seconds(value: float) -> str:
    return f"{value:.0f} s" if value >= 10 else f"{value:.1f} s"


class _Stream:
    """Folds a streamed completion into the same shape a non-streamed reply has."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.buffer = b""
        self.text: list[str] = []
        self.size = 0
        self.finish: str | None = None
        self.usage: dict[str, Any] = {}
        self.model: Any = None
        self.received = 0

    def feed(self, chunk: bytes) -> None:
        # Every event repeats its framing, so a long reply is far larger on the wire than as
        # text. The total still has to be bounded: it caps whatever else an event carries.
        self.received += len(chunk)
        if self.received > MAX_STREAM_BYTES:
            raise ProviderError("provider response exceeded the stream size limit")
        self.buffer += chunk
        *lines, self.buffer = self.buffer.split(b"\n")
        if len(self.buffer) > 2 * 1024 * 1024:
            raise ProviderError("provider response exceeded 2 MiB")
        for line in lines:
            self.line(line.strip())

    def line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        body = line[5:].strip()
        if not body or body == b"[DONE]":
            return
        try:
            event = json.loads(body)
        except ValueError as exc:
            raise ProviderError("provider returned invalid JSON") from exc
        if not isinstance(event, dict):
            return
        if event.get("error") or event.get("type") == "error":
            raise ProviderError("the provider reported an error while replying", "provider-error")
        if isinstance(event.get("model"), str):
            self.model = event["model"][:200]
        if self.provider == "anthropic":
            self.anthropic(event)
        else:
            self.completion(event)

    def keep_usage(self, usage: Any) -> None:
        # Only small numeric counters are ever used; nothing else the provider sends is kept.
        if isinstance(usage, dict):
            self.usage.update(
                {
                    k: v
                    for k, v in list(usage.items())[:50]
                    if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
                }
            )

    def add(self, text: Any) -> None:
        if isinstance(text, str):
            self.size += len(text)
            if self.size > 2 * 1024 * 1024:
                raise ProviderError("provider response exceeded 2 MiB")
            self.text.append(text)

    def completion(self, event: dict[str, Any]) -> None:
        if isinstance(event.get("usage"), dict):
            self.keep_usage(event["usage"])
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                self.add(delta.get("content"))
            if choice.get("finish_reason"):
                self.finish = choice["finish_reason"]

    def anthropic(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "message_start" and isinstance(event.get("message"), dict):
            message = event["message"]
            self.keep_usage(message.get("usage"))
            if isinstance(message.get("model"), str):
                self.model = message["model"][:200]
        elif kind == "content_block_delta" and isinstance(event.get("delta"), dict):
            self.add(event["delta"].get("text"))
        elif kind == "message_delta":
            if isinstance(event.get("delta"), dict) and event["delta"].get("stop_reason"):
                self.finish = event["delta"]["stop_reason"]
            self.keep_usage(event.get("usage"))

    def result(self) -> dict[str, Any]:
        self.line(self.buffer.strip())
        text = "".join(self.text)
        if self.provider == "anthropic":
            return {
                "stop_reason": self.finish,
                "content": [{"type": "text", "text": text}],
                "usage": self.usage,
                "model": self.model,
            }
        return {
            "choices": [{"finish_reason": self.finish, "message": {"content": text}}],
            "usage": self.usage,
            "model": self.model,
        }


class ModelProvider:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport

    async def request(
        self,
        connection: ModelConnection,
        key: str,
        method: str,
        path: str,
        *,
        timeout: int = 45,
        payload: dict[str, Any] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, Any]:
        result, _ = await self._exchange(
            connection, key, method, path, timeout=timeout, payload=payload, params=params
        )
        return result

    async def _exchange(
        self,
        connection: ModelConnection,
        key: str,
        method: str,
        path: str,
        *,
        timeout: int,
        payload: dict[str, Any] | None = None,
        params: dict[str, str | int] | None = None,
        body: AsyncIterator[bytes] | None = None,
        content_type: str = "application/json",
        max_seconds: float | None = None,
        max_reply_bytes: int = 2 * 1024 * 1024,
        noun: str = "model provider",
        limit_name: str = "Processing limits",
        length: int | None = None,
    ) -> tuple[dict[str, Any], CallStats]:
        """One HTTP call. `timeout` is a silence limit: how long the provider may send nothing,
        before the first byte and between pieces of a streamed reply. `body` streams a request
        body of raw bytes (`content_type` says what it is) instead of sending `payload`."""
        if connection.provider != "compatible" and not key:
            raise ProviderError("this provider requires an API key")
        headers = {"Content-Type": content_type}
        if length is not None:
            headers["Content-Length"] = str(length)
        if connection.provider == "anthropic":
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        elif key:
            headers["Authorization"] = f"Bearer {key}"
        stats = CallStats()
        seen_body = False
        try:
            async with asyncio.timeout(
                max(MAX_CALL_SECONDS if max_seconds is None else max_seconds, timeout)
            ):
                async with (
                    httpx.AsyncClient(
                        transport=self.transport,
                        timeout=timeout,
                        follow_redirects=False,
                        trust_env=False,
                    ) as client,
                    client.stream(
                        method,
                        connection.endpoint_url + path,
                        headers=headers,
                        json=payload if body is None else None,
                        content=body,
                        params=params,
                    ) as response,
                ):
                    if not response.is_success:
                        raise self._http_error(response.status_code, upload=body is not None)
                    event_stream = "text/event-stream" in response.headers.get("content-type", "")
                    stats.streamed = event_stream
                    data = bytearray()
                    collector = _Stream(connection.provider) if event_stream else None
                    async for chunk in response.aiter_bytes():
                        seen_body = True
                        stats.mark_first_byte()
                        if collector is not None:
                            collector.feed(chunk)
                        else:
                            data.extend(chunk)
                            if len(data) > max_reply_bytes:
                                raise ProviderError(
                                    "provider response exceeded "
                                    f"{max_reply_bytes // (1024 * 1024)} MiB"
                                )
            if collector is not None:
                return collector.result(), stats
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ProviderError("provider returned an invalid response")
            return result, stats
        except TimeoutError as exc:
            raise ProviderError(
                (
                    f"The transcription service was still working after "
                    f"{_seconds(stats.elapsed)}, the longest the app waits for one recording. "
                    "Split the recording or use a faster service."
                    if body is not None
                    else f"The model was still replying after {_seconds(stats.elapsed)}, the "
                    "longest the app waits for one call. Use a faster model or a shorter "
                    "maximum response."
                ),
                "provider-too-long",
            ) from exc
        except httpx.TimeoutException as exc:
            if isinstance(exc, httpx.ConnectTimeout):
                raise self._cannot_connect(stats, noun) from exc
            if not seen_body:
                raise ProviderError(
                    f"The {noun} accepted the request but sent nothing for "
                    f"{_seconds(stats.elapsed)} (wait limit {timeout} s). It is probably busy "
                    "with other requests or still loading the model. The app will try again "
                    f"later; raise the wait limit under {limit_name} if this repeats.",
                    "provider-no-response",
                    waiting=True,
                ) from exc
            raise ProviderError(
                f"The {noun} stopped sending partway through its reply: it went "
                f"quiet for {timeout} s after {_seconds(stats.elapsed)} in total. The app will "
                f"try again later; raise the wait limit under {limit_name} if this repeats.",
                "provider-stalled",
                waiting=True,
            ) from exc
        except httpx.ConnectError as exc:
            raise self._cannot_connect(stats, noun) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"The {noun} dropped the connection after {_seconds(stats.elapsed)}"
                + (" while replying." if seen_body else " before replying."),
                "provider-dropped",
            ) from exc
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError("provider returned invalid JSON") from exc

    @staticmethod
    def _cannot_connect(stats: CallStats, noun: str) -> ProviderError:
        return ProviderError(
            f"Could not connect to the {noun} (gave up after {_seconds(stats.elapsed)}). "
            "Check the connection address and that the server is running.",
            "provider-connect",
            waiting=True,
        )

    async def models(self, connection: ModelConnection, key: str) -> list[str]:
        models: set[str] = set()
        params: dict[str, str | int] = {}
        for _ in range(20):
            result = await self.request(connection, key, "GET", "/models", params=params)
            rows = result.get("data", [])
            if not isinstance(rows, list):
                raise ProviderError("provider returned an invalid model list")
            models.update(
                item["id"]
                for item in rows
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            )
            if not result.get("has_more"):
                return sorted(models)
            cursor = result.get("last_id")
            if not cursor or cursor == params.get("after_id"):
                raise ProviderError("provider model pagination did not advance")
            params = {"after_id": cursor, "limit": 100}
        raise ProviderError("model list exceeded 20 pages; use a manual model ID")

    async def embed(
        self,
        connection: ModelConnection,
        key: str,
        model: str,
        texts: list[str],
        *,
        timeout: int = 60,
    ) -> list[list[float]]:
        """One vector per text, in order, from an OpenAI-compatible `/embeddings` endpoint.

        The reply is checked before anything is trusted: one vector for each text, every
        number finite, every vector the same length.
        """
        if connection.provider == "anthropic":
            raise ProviderError(
                "Anthropic offers no embeddings endpoint. Choose an OpenAI or "
                "OpenAI-compatible connection.",
                "provider-no-embeddings",
            )
        try:
            result = await self.request(
                connection,
                key,
                "POST",
                "/embeddings",
                timeout=timeout,
                payload={"model": model, "input": texts, "encoding_format": "float"},
            )
        except ProviderError as exc:
            # 400, 413 and 422 are about the text sent (too long, unusable), not about the
            # service or the credentials; the caller sends less, then gives up on the text.
            if not exc.rule and str(exc) in {
                f"provider returned HTTP {code}" for code in (400, 413, 422)
            }:
                raise ProviderError(
                    f"The embeddings service refused the text it was sent ({str(exc)[-3:]}).",
                    "provider-rejected",
                ) from exc
            if not exc.rule and str(exc) == "provider returned HTTP 404":
                raise ProviderError(
                    "The embeddings service answered 404: no such model or address.",
                    "provider-not-found",
                ) from exc
            raise
        rows = result.get("data")
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise ProviderError(
                "The embeddings service did not return one vector for each text.",
                "provider-bad-embeddings",
            )
        ordered: list[list[float]] = [[] for _ in texts]
        seen: set[int] = set()
        for position, row in enumerate(rows):
            index = row.get("index", position) if isinstance(row, dict) else None
            vector = row.get("embedding") if isinstance(row, dict) else None
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < len(texts)
                or index in seen
                or not isinstance(vector, list)
                or not vector
                or len(vector) > MAX_EMBEDDING_DIMENSIONS
                or not all(
                    isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)
                    for x in vector
                )
            ):
                raise ProviderError(
                    "The embeddings service returned a vector that could not be used.",
                    "provider-bad-embeddings",
                )
            seen.add(index)
            ordered[index] = [float(x) for x in vector]
        if len({len(vector) for vector in ordered}) != 1:
            raise ProviderError(
                "The embeddings service returned vectors of different lengths.",
                "provider-bad-embeddings",
            )
        return ordered

    async def suggest(
        self,
        connection: ModelConnection,
        key: str,
        assignment: FeatureAssignment,
        system: str,
        evidence: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        content = json.dumps(evidence, ensure_ascii=False)
        if len(content) > assignment.max_input_chars:
            raise InputLimitError(len(content), assignment.max_input_chars)
        # The reply is streamed so that a slow but steady model is not cut off: the wait
        # limit counts silence, not total time.
        if connection.provider == "anthropic":
            payload = {
                "model": assignment.model,
                "max_tokens": assignment.max_output_tokens,
                "system": system,
                "messages": [{"role": "user", "content": content}],
                "stream": True,
            }
            result, stats = await self._exchange(
                connection, key, "POST", "/messages", timeout=assignment.timeout_s, payload=payload
            )
            stopped = result.get("stop_reason")
            finished = stopped == "end_turn"
            output = "".join(
                block.get("text", "")
                for block in result.get("content", [])
                if block.get("type") == "text"
            )
        else:
            limit_name = (
                "max_completion_tokens" if connection.provider == "openai" else "max_tokens"
            )
            payload = {
                "model": assignment.model,
                limit_name: assignment.max_output_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
                "response_format": {"type": "json_object"},
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            result, stats = await self._exchange(
                connection,
                key,
                "POST",
                "/chat/completions",
                timeout=assignment.timeout_s,
                payload=payload,
            )
            try:
                choice = result["choices"][0]
                stopped = choice["finish_reason"]
                finished = stopped == "stop"
                output = choice["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ProviderError("provider returned an invalid completion") from exc
        if not isinstance(output, str):
            output = ""
        if not finished:
            cut_short = stopped in {"length", "max_tokens"}
            raise ProviderError(
                "The model ran out of response space before it finished its answer. Raise "
                "Maximum response tokens, or use a model that spends less on thinking first."
                if cut_short
                else "The model stopped before it finished its answer.",
                "provider-incomplete",
                reply=output,
            )
        if not output.strip():
            raise ProviderError(
                "The model sent back an empty answer. A model that thinks first may have used "
                "all of its response tokens before answering; raise Maximum response tokens "
                "or use a model that thinks less.",
                "provider-empty-reply",
            )
        try:
            parsed = json.loads(output)
        except ValueError as exc:
            raise ProviderError(
                "The model's answer was not valid JSON.", "provider-not-json", reply=output
            ) from exc
        if not isinstance(parsed, dict):
            raise ProviderError(
                "The model's answer was JSON but not a JSON object.",
                "provider-not-json",
                reply=output,
            )
        raw = result.get("usage")
        # Only numeric usage fields are retained. Never reflect arbitrary remote metadata.
        usage: dict[str, Any] = {
            k: v
            for k, v in (raw if isinstance(raw, dict) else {}).items()
            if isinstance(v, int) and not isinstance(v, bool)
        }
        usage.update(stats.usage())
        reported = result.get("model")
        usage["model"] = assignment.model
        usage["provider_model_version"] = (
            reported[:200] if isinstance(reported, str) else "not supplied"
        )
        return parsed, usage

    @staticmethod
    def _http_error(status: int, *, upload: bool) -> ProviderError:
        if not upload:
            return ProviderError(f"provider returned HTTP {status}")
        hints = {
            400: "It refused the request; the recording may be in a format it cannot read.",
            401: "It refused the key. Check the connection's key.",
            403: "It refused the key. Check the connection's key.",
            404: "It has no transcription endpoint at that address, or does not know the model. "
            "Check the connection address and the model name.",
            413: "It refused the recording as too large.",
            415: "It cannot read this kind of recording.",
            422: "It could not use the request; the model name or the reply format may not be "
            "supported by this service.",
        }
        retryable = status >= 500 or status in (408, 429)
        detail = hints.get(
            status, "Check that the service is healthy." if retryable else "The request failed."
        )
        return ProviderError(
            f"The transcription service answered HTTP {status}. {detail}",
            f"transcription-http-{status}" if status in hints else "transcription-http",
            # A server error or a request to slow down is worth another try later; a refusal
            # of the request is not.
            waiting=retryable,
        )

    async def transcribe(
        self,
        connection: ModelConnection,
        key: str,
        assignment: FeatureAssignment,
        audio: Path,
        *,
        filename: str,
        mime_type: str,
        size_bytes: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Send one recording to `POST /audio/transcriptions`; return its speaker segments.

        The file goes out in chunks and is never held whole. The service may take many
        minutes to answer: `assignment.timeout_s` is how long it may stay silent, and
        `MAX_TRANSCRIBE_SECONDS` bounds the call overall.
        """
        if connection.provider == "anthropic":
            raise ProviderError(
                "Anthropic does not offer transcription. Choose an OpenAI-compatible connection.",
                "transcription-provider",
            )
        boundary = "coppermind-" + secrets.token_hex(16)
        fields = {"model": assignment.model, "response_format": assignment.response_format}
        if assignment.response_format == "diarized_json":
            fields["chunking_strategy"] = "auto"
        head = (
            b"".join(
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; "
                    f'name="{name}"\r\n\r\n{value}\r\n'
                ).encode()
                for name, value in fields.items()
            )
            + (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{_header_safe(filename)}"\r\nContent-Type: {_header_safe(mime_type)}'
                "\r\n\r\n"
            ).encode()
        )
        tail = f"\r\n--{boundary}--\r\n".encode()

        async def body() -> AsyncIterator[bytes]:
            yield head
            sent = 0
            with audio.open("rb") as stream:
                while chunk := await asyncio.to_thread(stream.read, UPLOAD_CHUNK_BYTES):
                    sent += len(chunk)
                    yield chunk
            if sent != size_bytes:
                raise ProviderError(
                    "The stored recording changed while it was being sent.", "recording-changed"
                )
            yield tail

        result, stats = await self._exchange(
            connection,
            key,
            "POST",
            "/audio/transcriptions",
            timeout=assignment.timeout_s,
            body=body(),
            content_type=f"multipart/form-data; boundary={boundary}",
            max_seconds=MAX_TRANSCRIBE_SECONDS,
            max_reply_bytes=MAX_TRANSCRIPT_BYTES,
            noun="transcription service",
            limit_name="Transcription",
            length=len(head) + size_bytes + len(tail),
        )
        transcript = parse_transcript(result)
        reported = result.get("usage")
        usage: dict[str, Any] = {
            k: v
            for k, v in (reported if isinstance(reported, dict) else {}).items()
            if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
        }
        usage.update(stats.usage())
        usage["model"] = assignment.model
        return transcript, usage


def _header_safe(value: str) -> str:
    return "".join("_" if c in '"\\\r\n' else c for c in value)


def _clock(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def parse_transcript(result: dict[str, Any]) -> dict[str, Any]:
    """The service's reply as ordered speaker segments, with speakers kept as given."""
    raw = result.get("segments")
    segments: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        text = item["text"].strip()
        if not text:
            continue
        speaker = item.get("speaker")
        segments.append(
            {
                "speaker": str(speaker).strip()[:80]
                if isinstance(speaker, str | int) and not isinstance(speaker, bool)
                else "",
                "start": _clock(item.get("start")),
                "end": _clock(item.get("end")),
                "text": text,
            }
        )
    if not segments:
        if str(result.get("text") or "").strip():
            raise ProviderError(
                "The transcription service sent text but no segments, so speakers cannot be "
                "told apart. Choose a service and reply format that label speakers.",
                "transcript-flat",
            )
        raise ProviderError(
            "The transcription service sent back no speech. The recording may be silent or in a "
            "format it could not read.",
            "transcript-empty",
        )
    if not any(segment["speaker"] for segment in segments):
        raise ProviderError(
            "The transcription service did not label any speaker. Choose a service and reply "
            "format that does, such as diarized_json.",
            "transcript-no-speakers",
        )
    language = result.get("language")
    return {
        "language": language[:40] if isinstance(language, str) else "",
        "duration": _clock(result.get("duration")),
        "segments": segments,
    }
