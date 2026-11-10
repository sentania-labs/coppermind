"""Search by meaning against PostgreSQL with pgvector, and a stand-in embeddings service.

The stand-in turns text into a vector with one axis per idea ("licensing" is the
words licence, permit and royalty), so a test can ask for a note by an idea its
words never state. Everything else is the real thing: the worker, the table, the
distance query and the fusion with full-text search.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
import sqlalchemy as sa
from coppermind_store import filing, indexer
from coppermind_store.embeddings import EmbeddingService
from coppermind_store.model_provider import ModelProvider
from coppermind_store.notes import LocalStore

from coppermind.context_config import ContextConfig, EmbeddingAssignment, ModelConnection
from coppermind.db.models import SearchDocument, SearchEmbedding
from coppermind.db.session import transaction
from coppermind.store_protocol import CreateNote, SearchQuery

IDEAS = [
    {"licensing", "license", "licence", "permit", "royalty", "royalties"},
    {"lunch", "sandwich", "sandwiches", "soup"},
    {"warehouse", "lease", "forklift"},
]


def embed_text(text: str) -> list[float]:
    words = {word.strip(".,!?").lower() for word in text.split()}
    vector = [float(len(words & idea)) for idea in IDEAS]
    return vector if any(vector) else [0.01, 0.01, 0.01]


class FakeService:
    """The `/v1/embeddings` stand-in, with switches to make it slow or broken."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body["input"])
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "no"})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": embed_text(text)}
                    for i, text in enumerate(body["input"])
                ],
                "model": body["model"],
            },
        )


@pytest.fixture
def service() -> FakeService:
    return FakeService()


@pytest.fixture
def configured(store: LocalStore, service: FakeService) -> Callable[..., None]:
    store.context.provider = ModelProvider(transport=httpx.MockTransport(service.handler))

    def configure(model: str = "fake-embed", attempts: int = 3, connection: str = "local") -> None:
        config = ContextConfig(
            connections=[
                ModelConnection(
                    id=name,
                    name=name,
                    provider="compatible",
                    base_url=f"http://{name}.test/v1",
                )
                for name in ("local", "other")
            ],
            embeddings=EmbeddingAssignment(
                connection=connection, model=model, enabled=True, attempts=attempts
            ),
        )
        store.context.save_config(config)

    return configure


def note(title: str, body: str) -> CreateNote:
    return CreateNote(
        title=title,
        body=body,
        folder="Notes/2026/2026-09",
        frontmatter={
            "date": "2026-09-08",
            "type": "reference",
            "context": "internal",
            "reviewed": True,
        },
    )


async def create(store: LocalStore, title: str, body: str):
    created = await store.create_note(note(title, body))
    await filing.file_reviewed(store)
    await indexer.catch_up(store)
    return created


async def settle(store: LocalStore) -> None:
    for _ in range(50):
        await store.embeddings.step()
        status = await store.embeddings.status()
        if status.state in {"idle", "waiting"}:
            return
    raise AssertionError("embedding never settled")


async def found(store: LocalStore, q: str, **options: object) -> list[tuple[str, str]]:
    page = await store.search(SearchQuery.model_validate({"q": q, **options}))
    return [(hit.title, hit.match) for hit in page.items]


async def vectors(store: LocalStore) -> int:
    async with store.session_factory() as session:
        return int(
            await session.scalar(sa.select(sa.func.count()).select_from(SearchEmbedding)) or 0
        )


async def test_meaning_finds_what_the_words_do_not(store, configured, service) -> None:
    await create(store, "Vendor call", "We agreed the permit fees and royalty schedule.")
    await create(store, "Team lunch", "Soup and sandwiches on Friday.")
    configured()

    # Before the worker has reached anything the answer is the full-text answer.
    assert await found(store, "licensing") == []

    await settle(store)
    status = await store.embeddings.status()
    assert (status.state, status.documents, status.embedded) == ("idle", 2, 2)

    assert await found(store, "licensing") == [("Vendor call", "meaning")]
    assert await found(store, "licensing", mode="keyword") == []
    assert await found(store, "royalty") == [("Vendor call", "both")]


async def test_without_a_connection_search_is_full_text(store, service) -> None:
    await create(store, "Vendor call", "We agreed the permit fees and royalty schedule.")
    assert await found(store, "royalty") == [("Vendor call", "keyword")]
    assert (await store.embeddings.status()).state == "off"
    assert service.calls == []


async def test_a_dead_service_never_breaks_search_and_is_backed_off(
    store, configured, service
) -> None:
    await create(store, "Vendor call", "We agreed the permit fees and royalty schedule.")
    configured()
    service.status = 503
    delay = await store.embeddings.step()
    assert delay > 0
    status = await store.embeddings.status()
    assert status.state == "waiting" and status.retry_at is not None and status.error
    calls = len(service.calls)
    # Search asks once, fails, answers from words, then leaves the service alone.
    assert await found(store, "royalty") == [("Vendor call", "keyword")]
    assert await found(store, "royalty") == [("Vendor call", "keyword")]
    assert len(service.calls) == calls + 1


async def test_edits_reembed_and_deletes_remove_vectors(store, configured, service) -> None:
    await create(store, "Vendor call", "We agreed the permit fees.")
    configured()
    await settle(store)
    assert await vectors(store) == 1

    async with store.session_factory() as session, session.begin():
        await session.execute(
            sa.update(SearchDocument).values(body="Soup for lunch.", version="v2")
        )
    await settle(store)
    assert await found(store, "sandwich") == [("Vendor call", "meaning")]

    async with store.session_factory() as session, session.begin():
        await session.execute(sa.delete(SearchDocument))
    assert await vectors(store) == 0


async def test_changing_the_model_reembeds_everything(store, configured, service) -> None:
    await create(store, "Vendor call", "We agreed the permit fees.")
    configured("model-a")
    await settle(store)
    first = len(service.calls)
    configured("model-b")
    await settle(store)
    assert len(service.calls) > first
    async with store.session_factory() as session:
        models = set((await session.scalars(sa.select(SearchEmbedding.model))).all())
    assert {tag.rsplit("|", 1)[1] for tag in models} == {"model-b"}


async def test_a_text_the_service_refuses_is_set_aside(store, configured, service) -> None:
    await create(store, "Vendor call", "We agreed the permit fees.")
    await create(store, "Team lunch", "Soup and sandwiches on Friday.")
    configured(attempts=1)
    original = service.handler

    def refuse_lunch(request: httpx.Request) -> httpx.Response:
        if "sandwiches" in request.content.decode():
            return httpx.Response(400, json={"error": "too long"})
        return original(request)

    store.context.provider = ModelProvider(transport=httpx.MockTransport(refuse_lunch))
    await settle(store)
    status = await store.embeddings.status()
    assert (status.documents, status.embedded, status.skipped) == (2, 1, 1)
    assert status.state == "idle"


async def test_a_missing_table_is_created_once_the_server_offers_pgvector(
    store, configured, service
) -> None:
    async with store.session_factory() as session:
        offered = await session.scalar(
            sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
        )
    if offered is None:
        pytest.skip("this PostgreSQL does not offer pgvector")
    await create(store, "Vendor call", "We agreed the permit fees and royalty schedule.")
    async with transaction(store.session_factory) as session:
        await session.execute(sa.text("DROP TABLE search_embeddings"))
    store.embeddings = EmbeddingService(store)
    configured()

    status = await store.embeddings.status()

    assert status.state != "unavailable"
    await settle(store)
    assert await found(store, "licensing") == [("Vendor call", "meaning")]


async def test_another_connection_serving_the_same_model_name_is_embedded_again(
    store, configured, service
) -> None:
    await create(store, "Vendor call", "We agreed the permit fees.")
    configured(connection="local")
    await settle(store)
    first = len(service.calls)

    configured(connection="other")
    await settle(store)

    assert len(service.calls) > first
    status = await store.embeddings.status()
    assert (status.documents, status.embedded) == (1, 1)
    async with store.session_factory() as session:
        tags = set((await session.scalars(sa.select(SearchEmbedding.model))).all())
    assert len(tags) == 1 and next(iter(tags)).startswith("other|")


async def attempt(store: LocalStore, **assignment: object) -> str:
    """Save through the same call Admin makes; returns the refusal, or empty when saved."""
    config = store.context.config()
    try:
        await store.embeddings.save(
            {
                "revision": config.revision,
                "assignment": {"connection": "local", "model": "m1", "enabled": True, **assignment},
            }
        )
    except ValueError as exc:
        return str(exc)
    return ""


@pytest.mark.parametrize(
    ("respond", "expected"),
    [
        (
            lambda request: httpx.Response(404, json={"error": "no such model"}),
            "The service couldn't find model ‘m1’. Check the model name or refresh "
            "the model list. Nothing was saved.",
        ),
        (
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused", request=request)),
            "Couldn't connect to local. Check its address and that the service is running. "
            "Nothing was saved.",
        ),
        (
            lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=request)),
            "The test timed out; nothing was saved. Check that the service is ready, then try "
            "again.",
        ),
        (
            lambda request: httpx.Response(422, json={"error": "not an embeddings model"}),
            "local did not accept the test request. Check that \u2018m1\u2019 is an embeddings "
            "model and that the connection address ends in /v1. Nothing was saved.",
        ),
        (
            lambda request: httpx.Response(200, json={"data": []}),
            "local did not accept the test request. Check that \u2018m1\u2019 is an embeddings "
            "model and that the connection address ends in /v1. Nothing was saved.",
        ),
        (
            lambda request: httpx.Response(401),
            "local refused the credentials. Check the API key on the connection. "
            "Nothing was saved.",
        ),
    ],
)
async def test_a_failed_test_says_what_happened_and_what_to_do(
    store, configured, respond, expected
) -> None:
    configured()
    store.context.provider = ModelProvider(transport=httpx.MockTransport(respond))
    before = store.context.config().embeddings

    assert await attempt(store, model="m1", enabled=True) == expected
    assert store.context.config().embeddings == before


async def test_the_wait_limit_is_saved_and_a_good_test_saves_everything(
    store, configured, service
) -> None:
    configured()
    assert await attempt(store, model="m2", timeout_s=90) == ""
    saved = store.context.config().embeddings
    assert (saved.model, saved.timeout_s, saved.enabled) == ("m2", 90, True)
