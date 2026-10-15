"""Bounded provider calls. Remote errors never expose headers or response bodies."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from coppermind.context_config import FeatureAssignment, ModelConnection


class ProviderError(ValueError):
    pass


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
        if connection.provider != "compatible" and not key:
            raise ProviderError("this provider requires an API key")
        headers = {"Content-Type": "application/json"}
        if connection.provider == "anthropic":
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        elif key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(
                    transport=self.transport,
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    async with client.stream(
                        method,
                        connection.endpoint_url + path,
                        headers=headers,
                        json=payload,
                        params=params,
                    ) as response:
                        if not response.is_success:
                            raise ProviderError(f"provider returned HTTP {response.status_code}")
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > 2 * 1024 * 1024:
                                raise ProviderError("provider response exceeded 2 MiB")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ProviderError("provider returned an invalid response")
            return result
        except (httpx.HTTPError, TimeoutError) as exc:
            raise ProviderError("provider connection failed or timed out") from exc
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError("provider returned invalid JSON") from exc

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
            raise ProviderError("input exceeds the configured limit; nothing was sent")
        if connection.provider == "anthropic":
            payload = {
                "model": assignment.model,
                "max_tokens": assignment.max_output_tokens,
                "system": system,
                "messages": [{"role": "user", "content": content}],
            }
            result = await self.request(
                connection, key, "POST", "/messages", timeout=assignment.timeout_s, payload=payload
            )
            if result.get("stop_reason") != "end_turn":
                raise ProviderError("model response was incomplete")
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
            }
            result = await self.request(
                connection,
                key,
                "POST",
                "/chat/completions",
                timeout=assignment.timeout_s,
                payload=payload,
            )
            try:
                choice = result["choices"][0]
                if choice["finish_reason"] != "stop":
                    raise ProviderError("model response was incomplete")
                output = choice["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ProviderError("provider returned an invalid completion") from exc
        try:
            parsed = json.loads(output)
            if not isinstance(parsed, dict):
                raise ValueError
            usage = result.get("usage", {})
            # Only numeric usage fields are retained. Never reflect arbitrary remote metadata.
            usage = {
                k: v for k, v in usage.items() if isinstance(v, int) and not isinstance(v, bool)
            }
            return parsed, usage
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProviderError("model did not return a JSON object") from exc
