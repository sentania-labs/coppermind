"""Embedding: passages, the provider contract, and what a question turns into."""

import json
from types import SimpleNamespace

import httpx
import pytest
from coppermind_store.embeddings import PASSAGE_CHARS, EmbeddingService, passage_text, passages
from coppermind_store.model_provider import ModelProvider, ProviderError
from coppermind_store.search import meaning_text

from coppermind.context_config import ContextConfig, EmbeddingAssignment, ModelConnection


def test_a_short_body_is_one_passage_and_an_empty_one_still_has_its_title():
    assert passages("A short note.") == [(0, 13)]
    assert passages("") == [(0, 0)]
    assert passages("   \n ") == [(0, 0)]
    assert passage_text("Vendor call", "", (0, 0)) == "Vendor call"


def test_a_long_body_is_cut_at_paragraph_ends_with_overlap_and_covers_everything():
    body = "\n\n".join(f"Paragraph {n}. " + "word " * 60 for n in range(30))
    spans = passages(body)
    assert len(spans) > 5
    assert all(0 < end - start <= PASSAGE_CHARS for start, end in spans)
    assert spans[0][0] == 0 and spans[-1][1] == len(body)
    for (_, previous_end), (next_start, _) in zip(spans, spans[1:], strict=False):
        assert next_start < previous_end  # overlap, so no thought is only ever cut in half
    # Most cuts fall where a paragraph ended.
    assert sum(body[end - 2 : end] == "\n\n" for _, end in spans[:-1]) >= len(spans) // 2


def test_a_body_with_no_break_is_still_cut():
    spans = passages("x" * (PASSAGE_CHARS * 3))
    assert len(spans) >= 3 and spans[-1][1] == PASSAGE_CHARS * 3


def test_the_title_is_embedded_with_each_passage():
    assert passage_text("Vendor call", "Body text. ", (0, 10)) == "Vendor call\n\nBody text."


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("licensing change", "licensing change"),
        ('"licensing change" -budget', "licensing change"),
        ("licensing or royalty", "licensing royalty"),
        ("only -negative -terms", "only"),
        ("-everything", ""),
    ],
)
def test_a_question_is_embedded_without_its_exclusions(q: str, expected: str):
    assert meaning_text(q) == expected


def connection(provider: str = "compatible") -> ModelConnection:
    return ModelConnection(
        id="lab",
        name="Lab",
        provider=provider,  # type: ignore[arg-type]
        base_url="http://embed.test/v1" if provider == "compatible" else "",
    )


def provider_with(handler) -> ModelProvider:
    return ModelProvider(transport=httpx.MockTransport(handler))


async def test_embed_sends_the_openai_request_and_returns_vectors_in_order():
    seen: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # Out of order on purpose: the index says which text each vector is for.
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
        )

    vectors = await provider_with(serve).embed(connection(), "key", "m", ["a", "b"])

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    request = seen[0]
    assert request.url == "http://embed.test/v1/embeddings"
    assert request.headers["authorization"] == "Bearer key"
    assert json.loads(request.content) == {
        "model": "m",
        "input": ["a", "b"],
        "encoding_format": "float",
    }


@pytest.mark.parametrize(
    "reply",
    [
        {"data": []},
        {"data": [{"index": 0, "embedding": [1.0]}]},
        {"data": [{"index": 0, "embedding": []}, {"index": 1, "embedding": []}]},
        {"data": [{"index": 0, "embedding": [1.0]}, {"index": 0, "embedding": [1.0]}]},
        {"data": [{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [1.0, 2.0]}]},
        {"data": [{"index": 0, "embedding": ["x"]}, {"index": 1, "embedding": [1.0]}]},
        {"data": [{"index": 0, "embedding": [True]}, {"index": 1, "embedding": [1.0]}]},
        {"data": "nope"},
    ],
)
async def test_a_reply_that_is_not_one_usable_vector_per_text_is_refused(reply):
    provider = provider_with(lambda request: httpx.Response(200, json=reply))
    with pytest.raises(ProviderError) as refused:
        await provider.embed(connection(), "", "m", ["a", "b"])
    assert refused.value.rule == "provider-bad-embeddings"


async def test_anthropic_has_no_embeddings():
    with pytest.raises(ProviderError) as refused:
        await provider_with(lambda request: httpx.Response(500)).embed(
            connection("anthropic"), "key", "m", ["a"]
        )
    assert refused.value.rule == "provider-no-embeddings"


@pytest.mark.parametrize(
    ("status", "rule"), [(400, "provider-rejected"), (413, "provider-rejected")]
)
async def test_a_refused_text_is_told_apart_from_a_failing_service(status, rule):
    provider = provider_with(lambda request: httpx.Response(status))
    with pytest.raises(ProviderError) as refused:
        await provider.embed(connection(), "", "m", ["a"])
    assert refused.value.rule == rule


@pytest.mark.parametrize("status", [401, 404, 429, 500, 503])
async def test_other_failures_are_not_blamed_on_the_text(status):
    provider = provider_with(lambda request: httpx.Response(status))
    with pytest.raises(ProviderError) as refused:
        await provider.embed(connection(), "", "m", ["a"])
    assert refused.value.rule != "provider-rejected"


async def test_a_silent_service_is_reported_as_one_to_wait_for():
    def hang(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError) as slow:
        await provider_with(hang).embed(connection(), "", "m", ["a"], timeout=5)
    assert slow.value.waiting


def test_a_saved_configuration_without_embeddings_still_loads_with_them_off():
    old = ContextConfig.model_validate({"revision": 3, "assignments": {"enrichment": {}}})
    assert old.embeddings == EmbeddingAssignment()
    assert old.embeddings.enabled is False
    # The enrichment assignment and its fingerprint are untouched by the new one.
    assert set(old.assignments) == {"enrichment"}


async def test_saving_from_admin_keeps_the_timeout_and_attempts_an_operator_set():
    asked: list[float] = []

    async def embed(_connection, _key, _model, _texts, *, timeout):
        asked.append(timeout)
        return [[0.1]]

    saved: list[ContextConfig] = []
    config = ContextConfig(
        connections=[connection()],
        embeddings=EmbeddingAssignment(connection="lab", model="old", timeout_s=300, attempts=5),
    )

    class Lock:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_):
            return None

    context = SimpleNamespace(
        config=lambda: config,
        connection=lambda _id: connection(),
        key=lambda _id: "secret",
        provider=SimpleNamespace(embed=embed),
        lock=Lock(),
        check_revision=lambda *_: None,
        save_config=saved.append,
    )
    service = EmbeddingService(SimpleNamespace(context=context))

    async def ready(*, refresh=False):
        return True

    service.table_ready = ready  # type: ignore[method-assign]

    await service.save(
        {"revision": 0, "assignment": {"connection": "lab", "model": " new ", "enabled": True}}
    )

    kept = saved[0].embeddings
    assert (kept.model, kept.enabled, kept.timeout_s, kept.attempts) == ("new", True, 300, 5)
    assert asked == [300]
