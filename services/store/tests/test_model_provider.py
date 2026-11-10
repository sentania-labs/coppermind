"""Provider contracts, credentials and bounded failure responses."""

import asyncio
import json
import time

import httpx
import pytest
from coppermind_store.model_provider import ModelProvider, ProviderError

from coppermind.context_config import FeatureAssignment, ModelConnection


@pytest.mark.parametrize("provider", ["anthropic", "openai", "compatible"])
async def test_discovery_and_json_contract(provider):
    seen = []

    def serve(request):
        seen.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        payload = json.loads(request.content)
        assert payload["model"] == "test-model"
        if provider == "anthropic":
            assert request.headers["x-api-key"] == "synthetic-key"
            assert request.headers["anthropic-version"] == "2023-06-01"
            return httpx.Response(
                200,
                json={
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": '{"ok":true}'}],
                    "usage": {"input_tokens": 10},
                },
            )
        assert request.headers["authorization"] == "Bearer synthetic-key"
        assert payload["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 10},
            },
        )

    connection = ModelConnection(
        id="test",
        name="Test",
        provider=provider,
        base_url="http://local.example/v1" if provider == "compatible" else "",
    )
    adapter = ModelProvider(httpx.MockTransport(serve))
    assert await adapter.models(connection, "synthetic-key") == ["test-model"]
    result, usage = await adapter.suggest(
        connection,
        "synthetic-key",
        FeatureAssignment(model="test-model"),
        "Return JSON",
        {"test": True},
    )
    assert result == {"ok": True}
    assert 10 in usage.values()
    assert len(seen) == 2


async def test_errors_do_not_echo_remote_secrets_or_follow_redirects():
    def serve(request):
        return httpx.Response(
            302, headers={"Location": "https://untrusted.example"}, text="secret remote diagnostic"
        )

    adapter = ModelProvider(httpx.MockTransport(serve))
    connection = ModelConnection(id="test", name="Test", provider="openai")
    with pytest.raises(ProviderError, match="HTTP 302") as caught:
        await adapter.models(connection, "synthetic-secret")
    assert "secret" not in str(caught.value)


async def test_oversized_input_is_not_sent():
    def serve(request):
        pytest.fail("oversized evidence must not be transmitted")

    adapter = ModelProvider(httpx.MockTransport(serve))
    connection = ModelConnection(id="test", name="Test", provider="openai")
    with pytest.raises(ProviderError, match="nothing was sent"):
        await adapter.suggest(
            connection,
            "synthetic",
            FeatureAssignment(max_input_chars=2000),
            "JSON",
            {"body": "x" * 2001},
        )


# --- A local model is slow: ~25 tokens/s after a queue delay. A real socket server is used
# because httpx's mock transport cannot enforce silence timeouts.


class SlowEngine:
    """Serves an OpenAI-style streamed completion at a fixed token rate after a queue delay."""

    def __init__(self, *, queue_delay=0.0, tokens=60, tps=25.0, stall_after=None, refuse=False):
        self.queue_delay, self.tokens, self.tps = queue_delay, tokens, tps
        self.stall_after, self.refuse = stall_after, refuse
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
            id="slow",
            name="Slow",
            provider="compatible",
            base_url=f"http://127.0.0.1:{self.port}/v1",
        )

    async def handle(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        length = int(
            next(
                line.split(b":")[1]
                for line in head.split(b"\r\n")
                if line.lower().startswith(b"content-length")
            )
        )
        self.requests.append(json.loads(await reader.readexactly(length)))
        await asyncio.sleep(self.queue_delay)
        writer.write(
            b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
            b"transfer-encoding: chunked\r\n\r\n"
        )

        async def send(event):
            data = b"data: " + event + b"\n\n"
            writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            await writer.drain()

        try:
            await send(
                json.dumps({"choices": [{"delta": {"content": '{"ok": true, "pad": "'}}]}).encode()
            )
            for i in range(self.tokens):
                if self.stall_after is not None and i == self.stall_after:
                    await asyncio.sleep(2)
                await asyncio.sleep(1 / self.tps)
                await send(json.dumps({"choices": [{"delta": {"content": "w"}}]}).encode())
            await send(
                json.dumps(
                    {"choices": [{"delta": {"content": '"}'}, "finish_reason": "stop"}]}
                ).encode()
            )
            await send(
                json.dumps(
                    {"choices": [], "usage": {"completion_tokens": self.tokens}, "model": "m-1"}
                ).encode()
            )
            await send(b"[DONE]")
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()


def _short_wait(seconds):
    # model_construct skips the 5 s floor so the tests stay fast.
    return FeatureAssignment.model_construct(
        **{**FeatureAssignment().__dict__, "model": "m", "timeout_s": seconds}
    )


async def test_a_slow_steady_reply_outlives_the_wait_limit():
    async with SlowEngine(queue_delay=1.2, tokens=60, tps=25) as engine:
        started = time.monotonic()
        result, usage = await ModelProvider().suggest(
            engine.connection, "", _short_wait(2), "JSON", {"x": 1}
        )
        took = time.monotonic() - started
    # The whole call took longer than the 2 s limit, but it was never silent for 2 s.
    assert took > 3 and result["ok"] is True
    assert engine.requests[0]["stream"] is True
    assert usage["streamed"] is True and usage["completion_tokens"] == 60
    assert usage["first_byte_ms"] >= 1100
    assert usage["elapsed_ms"] - usage["first_byte_ms"] >= 2000


async def test_silence_before_the_first_byte_is_a_distinct_waiting_error():
    async with SlowEngine(queue_delay=2) as engine:
        with pytest.raises(ProviderError) as caught:
            await ModelProvider().request(
                engine.connection, "", "POST", "/chat/completions", timeout=1, payload={}
            )
    assert caught.value.rule == "provider-no-response" and caught.value.waiting
    assert "nothing for" in str(caught.value) and "wait limit 1 s" in str(caught.value)


async def test_silence_in_the_middle_of_a_reply_is_a_distinct_waiting_error():
    async with SlowEngine(stall_after=5) as engine:
        with pytest.raises(ProviderError) as caught:
            await ModelProvider().request(
                engine.connection, "", "POST", "/chat/completions", timeout=1, payload={}
            )
    assert caught.value.rule == "provider-stalled" and caught.value.waiting
    assert "partway through" in str(caught.value)


async def test_a_refused_connection_is_a_distinct_error_that_does_not_wait():
    async with SlowEngine() as engine:
        connection = engine.connection
    with pytest.raises(ProviderError) as caught:
        await ModelProvider().request(connection, "", "POST", "/chat/completions", timeout=1)
    assert caught.value.rule == "provider-connect" and caught.value.waiting
    assert "Could not connect" in str(caught.value)


def _completion(content, finish="stop", **extra):
    return httpx.Response(
        200,
        json={"choices": [{"finish_reason": finish, "message": {"content": content}}], **extra},
    )


@pytest.mark.parametrize(
    ("reply", "finish", "rule", "kept"),
    [
        ("", "stop", "provider-empty-reply", ""),
        ("not json at all", "stop", "provider-not-json", "not json at all"),
        ("[1, 2]", "stop", "provider-not-json", "[1, 2]"),
        ('{"half":', "length", "provider-incomplete", '{"half":'),
    ],
)
async def test_a_refused_reply_names_the_reason_and_keeps_the_start_of_it(
    reply, finish, rule, kept
):
    adapter = ModelProvider(httpx.MockTransport(lambda request: _completion(reply, finish)))
    with pytest.raises(ProviderError) as caught:
        await adapter.suggest(
            ModelConnection(id="t", name="T", provider="openai"),
            "k",
            FeatureAssignment(model="m"),
            "JSON",
            {},
        )
    assert caught.value.rule == rule and caught.value.reply == kept
    assert not caught.value.waiting


async def test_the_reply_kept_on_a_failure_is_bounded():
    adapter = ModelProvider(httpx.MockTransport(lambda request: _completion("x" * 9000, "length")))
    with pytest.raises(ProviderError) as caught:
        await adapter.suggest(
            ModelConnection(id="t", name="T", provider="openai"),
            "k",
            FeatureAssignment(model="m"),
            "JSON",
            {},
        )
    assert caught.value.reply == "x" * 4000


async def test_a_gateway_that_ignores_streaming_still_works():
    adapter = ModelProvider(httpx.MockTransport(lambda request: _completion('{"ok": true}')))
    _, usage = await adapter.suggest(
        ModelConnection(id="t", name="T", provider="openai"),
        "k",
        FeatureAssignment(model="m"),
        "JSON",
        {},
    )
    assert usage["streamed"] is False


def _anthropic_events(*events):
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events
    )


def _anthropic_stream(stop_reason="end_turn"):
    # The event sequence is the one in Anthropic's streaming messages documentation.
    return _anthropic_events(
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [],
                "usage": {"input_tokens": 25, "output_tokens": 1},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "ping"},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": '{"ok": tr'},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "ue}"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 15},
        },
        {"type": "message_stop"},
    )


def _anthropic_adapter(body):
    def serve(request):
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    return ModelProvider(httpx.MockTransport(serve))


async def _suggest_anthropic(adapter):
    return await adapter.suggest(
        ModelConnection(id="a", name="A", provider="anthropic"),
        "k",
        FeatureAssignment(model="claude-test"),
        "JSON",
        {},
    )


async def test_an_anthropic_event_stream_is_folded_into_one_reply():
    result, usage = await _suggest_anthropic(_anthropic_adapter(_anthropic_stream()))
    assert result == {"ok": True}
    assert usage["streamed"] is True
    assert usage["input_tokens"] == 25 and usage["output_tokens"] == 15


async def test_an_anthropic_stream_cut_off_by_the_token_limit_is_incomplete():
    with pytest.raises(ProviderError) as caught:
        await _suggest_anthropic(_anthropic_adapter(_anthropic_stream("max_tokens")))
    assert caught.value.rule == "provider-incomplete"
    assert caught.value.reply == '{"ok": true}'


async def test_a_reply_that_never_ends_stops_at_the_longest_call_allowed(monkeypatch):
    monkeypatch.setattr("coppermind_store.model_provider.MAX_CALL_SECONDS", 0.5)
    # Steady output, so the 1 s silence limit never fires; only the overall cap can.
    async with SlowEngine(tokens=400, tps=25) as engine:
        with pytest.raises(ProviderError) as caught:
            await ModelProvider().request(
                engine.connection, "", "POST", "/chat/completions", timeout=1, payload={}
            )
    assert caught.value.rule == "provider-too-long" and not caught.value.waiting


async def test_admin_waits_for_a_slow_model_test_but_not_for_other_actions():
    from coppermind.store_client import MODEL_TEST_TIMEOUT_S, HttpStoreClient

    waits = {}

    def serve(request):
        waits[request.url.path.rsplit("/", 1)[1]] = request.extensions["timeout"]["read"]
        return httpx.Response(200, json={})

    client = HttpStoreClient("http://store", "t", timeout=30, transport=httpx.MockTransport(serve))
    await client.context_action("test-model", {})
    await client.context_action("snapshot", {})
    assert waits == {"test-model": MODEL_TEST_TIMEOUT_S, "snapshot": 30}
    assert MODEL_TEST_TIMEOUT_S > 120 + 30


def _sse(events):
    body = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


async def test_a_stream_of_oversized_metadata_events_is_cut_off_by_total_size():
    pad = "x" * (1024 * 1024)
    events = [{"choices": [], "usage": {"note": pad, "n": i}} for i in range(20)]
    adapter = ModelProvider(httpx.MockTransport(lambda request: _sse(events)))
    with pytest.raises(ProviderError, match="stream size limit"):
        await adapter.suggest(
            ModelConnection(id="t", name="T", provider="openai"),
            "k",
            FeatureAssignment(model="m"),
            "JSON",
            {},
        )


async def test_streamed_metadata_other_than_counters_is_not_kept():
    events = [
        {"choices": [{"delta": {"content": '{"ok": true}'}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"completion_tokens": 7, "blob": "y" * 5000}, "model": "m" * 900},
    ]
    adapter = ModelProvider(httpx.MockTransport(lambda request: _sse(events)))
    _, usage = await adapter.suggest(
        ModelConnection(id="t", name="T", provider="openai"),
        "k",
        FeatureAssignment(model="m"),
        "JSON",
        {},
    )
    assert usage["completion_tokens"] == 7 and "blob" not in usage
    assert len(usage["provider_model_version"]) == 200
