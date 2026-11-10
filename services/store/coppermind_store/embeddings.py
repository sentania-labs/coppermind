"""Search by meaning: embed what the index holds, and embed the question.

A background worker walks `search_documents` and gives every entry whose text
has no vectors from the current model passages and vectors, newest first. It is
one slow loop, deliberately apart from indexing and filing: a note is
searchable by its words the moment it is indexed, and by its meaning when the
worker reaches it. Nothing here is ever awaited by a write.

The embeddings service is usually a local model and often slow. Every call is
bounded (a small batch, a silence limit), a timeout halves the batch before the
next try, and a failing service is retried with a growing wait instead of a
tight loop. The backfill of existing content is the same loop, so it resumes by
itself after a restart: what is embedded is read from the table, not remembered.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from coppermind.context_config import EmbeddingAssignment, ModelConnection
from coppermind.db.models import SearchDocument, SearchEmbedding, Vector
from coppermind.db.session import transaction
from coppermind.logging import get_logger
from coppermind.store_protocol import EmbeddingStatus
from coppermind_store.model_provider import ProviderError

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore

log = get_logger("coppermind-store")

# A passage is about 1,200 characters (a few hundred tokens), which fits the
# 512-token window of the small local embedding models, with a little overlap
# so a thought cut at the edge is whole in one of its neighbours.
PASSAGE_CHARS = 1200
PASSAGE_OVERLAP = 150
# How many passages one request carries at most, and the biggest request body we
# build. A timeout lowers the first; it climbs back after a document succeeds.
BATCH_MAX = 16
# A question is embedded with this much patience, so search never waits long on
# a model that is busy backfilling.
QUERY_TIMEOUT_S = 10
# After a failed question embedding, searches skip the model for this long.
QUERY_COOLDOWN_S = 30
QUERY_CACHE_SIZE = 64
IDLE_S = 15.0
BACKOFF_FIRST_S = 15.0
BACKOFF_MAX_S = 900.0
# A document the service refuses this many times is left alone until its text
# changes or the configuration does.
SKIP_AFTER = 3


def passages(body: str) -> list[tuple[int, int]]:
    """Where each passage of `body` starts and ends, breaking at paragraph or line ends.

    Always at least one passage, so an empty body still has its title embedded.
    """
    if not body.strip():
        return [(0, 0)]
    spans: list[tuple[int, int]] = []
    start, size = 0, len(body)
    while start < size:
        end = min(start + PASSAGE_CHARS, size)
        if end < size:
            floor = start + PASSAGE_CHARS * 6 // 10
            for mark in ("\n\n", "\n", ". ", " "):
                cut = body.rfind(mark, floor, end)
                if cut != -1:
                    end = cut + len(mark)
                    break
        if body[start:end].strip():
            spans.append((start, end))
        if end >= size:
            break
        start = max(end - PASSAGE_OVERLAP, start + 1)
    return spans or [(0, 0)]


def passage_text(title: str, body: str, span: tuple[int, int]) -> str:
    """What is embedded for a passage: the title, so a passage knows what it belongs to."""
    return f"{title.strip()}\n\n{body[span[0] : span[1]].strip()}".strip()


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def vector_value(vector: list[float]) -> Any:
    """A bound `vector`, sent as text so no driver codec is needed."""
    return sa.cast(sa.cast(sa.literal(vector_literal(vector)), sa.Text), Vector())


@dataclass(frozen=True)
class QueryVector:
    vector: list[float]
    model: str


@dataclass(frozen=True)
class _Pending:
    kind: str
    ref_id: str
    version: str
    title: str
    body: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.ref_id, self.version)


class EmbeddingService:
    def __init__(self, store: LocalStore) -> None:
        self.store = store
        self._batch = BATCH_MAX
        self._error = ""
        self._retry_at: datetime | None = None
        self._failures = 0
        self._refused: dict[tuple[str, str, str], int] = {}
        self._skipped: set[tuple[str, str, str]] = set()
        self._working = False
        self._config_key: tuple[str, str] = ("", "")
        self._queries: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
        self._query_down_until = 0.0
        self._table_ok: bool | None = None
        self._table_checked = 0.0

    # --- configuration ---------------------------------------------------------------

    def assignment(self) -> EmbeddingAssignment:
        return self.store.context.config().embeddings

    def _connection(self, assignment: EmbeddingAssignment) -> tuple[ModelConnection, str]:
        connection = self.store.context.connection(assignment.connection)
        return connection, self.store.context.key(connection.id)

    @staticmethod
    def active(assignment: EmbeddingAssignment) -> bool:
        return bool(
            assignment.enabled
            and not assignment.paused
            and assignment.connection
            and assignment.model
        )

    def _follow_config(self, assignment: EmbeddingAssignment) -> None:
        """A different connection or model starts over: what was refused may now be accepted."""
        key = (assignment.connection, assignment.model)
        if key != self._config_key:
            self._config_key = key
            self._refused.clear()
            self._skipped.clear()
            self._failures = 0
            self._error = ""
            self._retry_at = None
            self._batch = BATCH_MAX
            self._queries.clear()
            self._query_down_until = 0.0

    async def table_ready(self) -> bool:
        """Whether the database has pgvector's table; checked again each minute while it has not."""
        now = time.monotonic()
        if self._table_ok or (self._table_ok is False and now - self._table_checked < 60):
            return bool(self._table_ok)
        try:
            async with self.store.session_factory() as session:
                found = await session.scalar(sa.text("SELECT to_regclass('search_embeddings')"))
        except (SQLAlchemyError, OSError):
            return False
        self._table_ok, self._table_checked = found is not None, now
        return self._table_ok

    # --- the worker ------------------------------------------------------------------

    async def run(self) -> None:
        while True:
            try:
                delay = await self.step()
            except asyncio.CancelledError:
                raise
            except (SQLAlchemyError, OSError) as exc:
                log.warning("embedding worker could not reach the database", error=str(exc))
                delay = 30.0
            except Exception:  # noqa: BLE001 - the loop must outlive any one document
                log.exception("embedding worker failed")
                delay = 60.0
            await asyncio.sleep(delay)

    async def step(self) -> float:
        """Embed at most one document. Returns how long to rest before the next step."""
        assignment = self.assignment()
        self._follow_config(assignment)
        self._working = False
        if not self.active(assignment) or not await self.table_ready():
            return IDLE_S
        try:
            connection, key = self._connection(assignment)
        except ValueError:
            self._error = "The connection this model uses no longer exists."
            return 60.0
        if self._retry_at is not None and self._retry_at > datetime.now(UTC):
            return min(IDLE_S, (self._retry_at - datetime.now(UTC)).total_seconds())
        pending = await self._next(assignment.model)
        if pending is None:
            self._error, self._retry_at, self._failures = "", None, 0
            await self._forget_other_models(assignment.model)
            return IDLE_S
        self._working = True
        try:
            vectors = await self._embed(pending, connection, key, assignment)
        except ProviderError as exc:
            return self._failed(pending, exc, assignment)
        if await self._save(pending, assignment.model, vectors):
            self._error, self._retry_at, self._failures = "", None, 0
            self._refused.pop(pending.key, None)
            self._batch = min(BATCH_MAX, self._batch * 2)
        return 0.05

    async def _next(self, model: str) -> _Pending | None:
        current = (
            sa.select(sa.literal(1))
            .select_from(SearchEmbedding)
            .where(
                SearchEmbedding.kind == SearchDocument.kind,
                SearchEmbedding.ref_id == SearchDocument.ref_id,
                SearchEmbedding.doc_version == SearchDocument.version,
                SearchEmbedding.model == model,
                SearchEmbedding.chunk_index == 0,
            )
            .exists()
        )
        statement = (
            sa.select(
                SearchDocument.kind,
                SearchDocument.ref_id,
                SearchDocument.version,
                SearchDocument.title,
                SearchDocument.body,
            )
            .where(~current)
            .order_by(SearchDocument.indexed_at.desc(), SearchDocument.kind, SearchDocument.ref_id)
            .limit(len(self._skipped) + 1)
        )
        async with self.store.session_factory() as session:
            rows = (await session.execute(statement)).all()
        for row in rows:
            candidate = _Pending(row.kind, row.ref_id, row.version, row.title, row.body)
            if candidate.key not in self._skipped:
                return candidate
        return None

    async def _embed(
        self,
        pending: _Pending,
        connection: ModelConnection,
        key: str,
        assignment: EmbeddingAssignment,
    ) -> list[tuple[tuple[int, int], list[float]]]:
        spans = passages(pending.body)
        texts = [passage_text(pending.title, pending.body, span) for span in spans]
        vectors: list[list[float]] = []
        provider = self.store.context.provider
        position = 0
        while position < len(texts):
            batch = texts[position : position + self._batch]
            try:
                vectors += await provider.embed(
                    connection, key, assignment.model, batch, timeout=assignment.timeout_s
                )
            except ProviderError as exc:
                # A slow service is asked for less next time; one refused text is found by
                # asking for fewer at a time.
                if self._batch > 1 and (exc.waiting or exc.rule == "provider-rejected"):
                    self._batch = max(1, self._batch // 2)
                    if exc.rule == "provider-rejected":
                        continue
                raise
            position += len(batch)
        return list(zip(spans, vectors, strict=True))

    def _failed(
        self, pending: _Pending, exc: ProviderError, assignment: EmbeddingAssignment
    ) -> float:
        if exc.rule == "provider-rejected" and self._batch == 1:
            refusals = self._refused.get(pending.key, 0) + 1
            self._refused[pending.key] = refusals
            if refusals >= min(assignment.attempts, SKIP_AFTER):
                self._skipped.add(pending.key)
                log.warning(
                    "embeddings service refused a document; leaving it until it changes",
                    kind=pending.kind,
                    ref_id=pending.ref_id,
                )
                return 1.0
        self._failures += 1
        wait = min(BACKOFF_MAX_S, BACKOFF_FIRST_S * 2 ** (self._failures - 1))
        self._error = str(exc)
        self._retry_at = datetime.now(UTC) + timedelta(seconds=wait)
        log.warning("embeddings service did not answer", error=str(exc), retry_in_s=wait)
        return min(IDLE_S, wait)

    async def _save(
        self,
        pending: _Pending,
        model: str,
        vectors: list[tuple[tuple[int, int], list[float]]],
    ) -> bool:
        """Replace the document's vectors, unless its text changed while they were made."""
        now = datetime.now(UTC)
        rows = [
            {
                "kind": pending.kind,
                "ref_id": pending.ref_id,
                "chunk_index": index,
                "doc_version": pending.version,
                "model": model,
                "start_char": span[0],
                "end_char": span[1],
                "embedding": vector_value(vector),
                "embedded_at": now,
            }
            for index, (span, vector) in enumerate(vectors)
        ]
        async with transaction(self.store.session_factory) as session:
            current = await session.scalar(
                sa.select(SearchDocument.version)
                .where(SearchDocument.kind == pending.kind, SearchDocument.ref_id == pending.ref_id)
                .with_for_update(read=True)
            )
            if current != pending.version:
                return False
            await session.execute(
                sa.delete(SearchEmbedding).where(
                    SearchEmbedding.kind == pending.kind, SearchEmbedding.ref_id == pending.ref_id
                )
            )
            await session.execute(sa.insert(SearchEmbedding).values(rows))
        return True

    async def _forget_other_models(self, model: str) -> None:
        """Once everything has current vectors, vectors of earlier models are dead weight."""
        async with transaction(self.store.session_factory) as session:
            await session.execute(sa.delete(SearchEmbedding).where(SearchEmbedding.model != model))

    # --- the question ----------------------------------------------------------------

    async def query_vector(self, text: str) -> QueryVector | None:
        """The vector for a question, or None when meaning is not available right now.

        None is never an error: search then answers from words alone. After a failure the
        model is left alone for a short while, so a stalled service costs one slow search,
        not every search.
        """
        assignment = self.assignment()
        if not self.active(assignment) or not text.strip():
            return None
        self._follow_config(assignment)
        if time.monotonic() < self._query_down_until or not await self.table_ready():
            return None
        cached = self._queries.get((assignment.model, text))
        if cached is not None:
            self._queries.move_to_end((assignment.model, text))
            return QueryVector(cached, assignment.model)
        try:
            connection, key = self._connection(assignment)
            (vector,) = await self.store.context.provider.embed(
                connection,
                key,
                assignment.model,
                [text],
                timeout=min(assignment.timeout_s, QUERY_TIMEOUT_S),
            )
        except (ProviderError, ValueError, OSError) as exc:
            self._query_down_until = time.monotonic() + QUERY_COOLDOWN_S
            log.warning("could not embed the question; answering from words alone", error=str(exc))
            return None
        self._queries[(assignment.model, text)] = vector
        while len(self._queries) > QUERY_CACHE_SIZE:
            self._queries.popitem(last=False)
        return QueryVector(vector, assignment.model)

    # --- what Admin shows ------------------------------------------------------------

    async def status(self) -> EmbeddingStatus:
        assignment = self.assignment()
        if not assignment.enabled or not assignment.model:
            return EmbeddingStatus(state="off", model=assignment.model)
        if not await self.table_ready():
            return EmbeddingStatus(
                state="unavailable",
                model=assignment.model,
                error=(
                    "This database does not have the pgvector extension, so meaning search "
                    "cannot store vectors. Use the PostgreSQL image the quickstart names, or "
                    "install pgvector, then restart."
                ),
            )
        model = assignment.model
        try:
            async with self.store.session_factory() as session:
                documents = await session.scalar(
                    sa.select(sa.func.count()).select_from(SearchDocument)
                )
                embedded = await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(SearchEmbedding)
                    .join(
                        SearchDocument,
                        sa.and_(
                            SearchDocument.kind == SearchEmbedding.kind,
                            SearchDocument.ref_id == SearchEmbedding.ref_id,
                            SearchDocument.version == SearchEmbedding.doc_version,
                        ),
                    )
                    .where(SearchEmbedding.model == model, SearchEmbedding.chunk_index == 0)
                )
                count = await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(SearchEmbedding)
                    .where(SearchEmbedding.model == model)
                )
        except (SQLAlchemyError, OSError):
            return EmbeddingStatus(state="idle", model=model)
        documents, embedded = int(documents or 0), int(embedded or 0)
        waiting = self._retry_at is not None and self._retry_at > datetime.now(UTC)
        if assignment.paused:
            state = "paused"
        elif waiting:
            state = "waiting"
        elif embedded + len(self._skipped) >= documents:
            state = "idle"
        else:
            state = "working"
        return EmbeddingStatus(
            state=state,  # type: ignore[arg-type]
            model=model,
            documents=documents,
            embedded=embedded,
            passages=int(count or 0),
            skipped=len(self._skipped),
            error=self._error if not assignment.paused else "",
            retry_at=self._retry_at if waiting else None,
        )

    # --- Admin actions (called through the context operations) ------------------------

    async def view(self, _: dict[str, Any]) -> dict[str, Any]:
        context = self.store.context
        config = context.config()
        discovery = context.read("discovery", {})
        return {
            "revision": config.revision,
            "embeddings": config.embeddings.model_dump(),
            "connections": [
                {"id": item.id, "name": item.name, "provider": item.provider}
                for item in config.connections
                if item.provider != "anthropic"
            ],
            "discovery": {
                item.id: discovery.get(context.storage_identity(item.id), [])
                for item in config.connections
            },
            "status": (await self.status()).model_dump(mode="json"),
        }

    async def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Save the assignment. Turning it on first asks the service for one vector."""
        assignment = EmbeddingAssignment.model_validate(payload["assignment"])
        assignment.model = assignment.model.strip()
        context = self.store.context
        if assignment.enabled:
            if not assignment.connection:
                raise ValueError("Choose a connection before turning meaning search on.")
            if not assignment.model:
                raise ValueError("Enter the embeddings model before turning meaning search on.")
        if assignment.connection:
            connection = context.connection(assignment.connection)
            if connection.provider == "anthropic":
                raise ValueError(
                    "Anthropic offers no embeddings. Choose an OpenAI or OpenAI-compatible "
                    "connection."
                )
            if assignment.enabled:
                # Asked before the configuration lock is taken: a slow model must not hold
                # every other settings change while it answers.
                try:
                    await context.provider.embed(
                        connection,
                        context.key(connection.id),
                        assignment.model,
                        ["Coppermind connection test"],
                        timeout=assignment.timeout_s,
                    )
                except ProviderError as exc:
                    raise ValueError(
                        f"The embeddings model did not answer a test, so nothing was saved. {exc}"
                    ) from exc
        async with context.lock:
            config = context.config()
            context.check_revision(config, payload)
            config.embeddings = assignment
            context.save_config(config)
        self._follow_config(assignment)
        return {"saved": "embeddings"}
