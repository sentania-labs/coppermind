"""Provider contracts, credentials and bounded failure responses."""

import json

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
