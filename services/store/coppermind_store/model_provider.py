"""Bounded provider calls. Remote errors never expose headers or response bodies."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from coppermind.context_config import FeatureAssignment, ModelConnection

# The longest the app waits for one model call, however steadily the reply arrives. The
# per-job wait limit is a silence limit; this stops a trickle from holding the single
# worker forever.
MAX_CALL_SECONDS = 3600
# How much of a model's reply is kept on a failed job so Inspect can show why it failed.
REPLY_KEPT_CHARS = 4000


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

    def feed(self, chunk: bytes) -> None:
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
            self.model = event["model"]
        if self.provider == "anthropic":
            self.anthropic(event)
        else:
            self.completion(event)

    def add(self, text: Any) -> None:
        if isinstance(text, str):
            self.size += len(text)
            if self.size > 2 * 1024 * 1024:
                raise ProviderError("provider response exceeded 2 MiB")
            self.text.append(text)

    def completion(self, event: dict[str, Any]) -> None:
        if isinstance(event.get("usage"), dict):
            self.usage.update(event["usage"])
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
            self.usage.update(message.get("usage") or {})
            if isinstance(message.get("model"), str):
                self.model = message["model"]
        elif kind == "content_block_delta" and isinstance(event.get("delta"), dict):
            self.add(event["delta"].get("text"))
        elif kind == "message_delta":
            if isinstance(event.get("delta"), dict) and event["delta"].get("stop_reason"):
                self.finish = event["delta"]["stop_reason"]
            self.usage.update(event.get("usage") or {})

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
    ) -> tuple[dict[str, Any], CallStats]:
        """One HTTP call. `timeout` is a silence limit: how long the provider may send nothing,
        before the first byte and between pieces of a streamed reply."""
        if connection.provider != "compatible" and not key:
            raise ProviderError("this provider requires an API key")
        headers = {"Content-Type": "application/json"}
        if connection.provider == "anthropic":
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        elif key:
            headers["Authorization"] = f"Bearer {key}"
        stats = CallStats()
        seen_body = False
        try:
            async with asyncio.timeout(max(MAX_CALL_SECONDS, timeout)):
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
                        json=payload,
                        params=params,
                    ) as response,
                ):
                    if not response.is_success:
                        raise ProviderError(f"provider returned HTTP {response.status_code}")
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
                            if len(data) > 2 * 1024 * 1024:
                                raise ProviderError("provider response exceeded 2 MiB")
            if collector is not None:
                return collector.result(), stats
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ProviderError("provider returned an invalid response")
            return result, stats
        except TimeoutError as exc:
            raise ProviderError(
                f"The model was still replying after {_seconds(stats.elapsed)}, the longest the "
                "app waits for one call. Use a faster model or a shorter maximum response.",
                "provider-too-long",
            ) from exc
        except httpx.TimeoutException as exc:
            if isinstance(exc, httpx.ConnectTimeout):
                raise self._cannot_connect(stats, timeout) from exc
            if not seen_body:
                raise ProviderError(
                    f"The model provider accepted the request but sent nothing for "
                    f"{_seconds(stats.elapsed)} (wait limit {timeout} s). It is probably busy "
                    "with other requests or still loading the model. The app will try again "
                    "later; raise the wait limit under Processing limits if this repeats.",
                    "provider-no-response",
                    waiting=True,
                ) from exc
            raise ProviderError(
                f"The model provider stopped sending partway through its reply: it went "
                f"quiet for {timeout} s after {_seconds(stats.elapsed)} in total. The app will "
                "try again later; raise the wait limit under Processing limits if this repeats.",
                "provider-stalled",
                waiting=True,
            ) from exc
        except httpx.ConnectError as exc:
            raise self._cannot_connect(stats, timeout) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"The model provider dropped the connection after {_seconds(stats.elapsed)}"
                + (" while replying." if seen_body else " before replying."),
                "provider-dropped",
            ) from exc
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError("provider returned invalid JSON") from exc

    @staticmethod
    def _cannot_connect(stats: CallStats, timeout: int) -> ProviderError:
        return ProviderError(
            f"Could not connect to the model provider (gave up after {_seconds(stats.elapsed)}). "
            "Check the connection address and that the model server is running.",
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
