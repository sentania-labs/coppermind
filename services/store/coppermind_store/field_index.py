"""Typed metadata indexing with atomic generations and durable job status."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from coppermind.atomicio import atomic_write_text
from coppermind.db.models import FieldIndexGeneration, Note, NoteFieldValue
from coppermind.field_filters import canonical_filter, filter_digest, validate_filter
from coppermind.ids import new_id
from coppermind.store_protocol import FieldIndexNotReady, NoteQuery, ValidationFailed

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore


def definitions(store: LocalStore) -> list[dict[str, Any]]:
    return [
        dict(name=k.name, kind=k.kind, vocabulary=k.vocabulary) for k in store.control.schema().keys
    ]


def query_context(query: NoteQuery, schema_revision: int) -> str:
    payload = query.model_dump(mode="json", by_alias=True, exclude={"cursor", "limit"})
    if query.filter:
        payload["filter"] = canonical_filter(query.filter)
    return filter_digest({"query": payload, "schema_revision": schema_revision})


async def conditions(store: LocalStore, query: NoteQuery, session: AsyncSession) -> list[Any]:
    if query.filter is None:
        return []
    schema = store.control.schema()
    try:
        clauses = validate_filter(query.filter, schema).all
    except ValueError as exc:
        raise ValidationFailed([str(exc)]) from exc
    generation = await session.scalar(
        sa.select(FieldIndexGeneration).where(FieldIndexGeneration.active)
    )
    available = {k["name"]: k for k in generation.definitions} if generation else {}
    needed = {k.name: dict(name=k.name, kind=k.kind, vocabulary=k.vocabulary) for k in schema.keys}
    if not generation or any(available.get(c.field) != needed[c.field] for c in clauses):
        raise FieldIndexNotReady(store.field_index.target_id())
    result = []
    kinds = {k.name: k.kind for k in schema.keys}
    for clause in clauses:
        value = sa.select(NoteFieldValue.note_id).where(
            NoteFieldValue.generation == generation.id,
            NoteFieldValue.note_id == Note.id,
            NoteFieldValue.field == clause.field,
            NoteFieldValue.invalid_reason.is_(None),
        )
        if kinds[clause.field] == "date":
            day = date.fromisoformat(clause.value)
            condition = (
                NoteFieldValue.date_value >= day
                if clause.op == "gte"
                else NoteFieldValue.date_value <= day
                if clause.op == "lte"
                else NoteFieldValue.date_value == day
            )
            value = value.where(condition)
            if clause.field == schema.role("date_key"):
                note_condition = (
                    Note.date >= day
                    if clause.op == "gte"
                    else Note.date <= day
                    if clause.op == "lte"
                    else Note.date == day
                )
                invalid_date = sa.select(NoteFieldValue.note_id).where(
                    NoteFieldValue.generation == generation.id,
                    NoteFieldValue.note_id == Note.id,
                    NoteFieldValue.field == clause.field,
                    NoteFieldValue.invalid_reason.is_not(None),
                )
                result.append(sa.and_(~invalid_date.exists(), note_condition))
                continue
        else:
            value = value.where(
                NoteFieldValue.value.contains(sa.literal(clause.value, type_=JSONB)),
                NoteFieldValue.value == sa.literal(clause.value, type_=JSONB),
            )
        result.append(value.exists())
    return result


class FieldIndex:
    def __init__(self, store: LocalStore) -> None:
        self.store = store
        self.path = store.control.store.state_dir / "field-index-job.json"
        self.lock = asyncio.Lock()
        self.request_path = self.path.with_name("field-index-request")

    def target_id(self) -> str:
        return filter_digest(definitions(self.store))

    def receipt(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "state": "queued",
                "job_id": self.target_id(),
                "processed": 0,
                "rebuild_requested": bool(self.pending_request()),
            }
        return dict(
            json.loads(self.path.read_bytes()), rebuild_requested=bool(self.pending_request())
        )

    def write(self, receipt: dict[str, Any]) -> None:
        atomic_write_text(self.path, json.dumps(receipt), mode=0o600)

    async def snapshot(self, offset: int = 0) -> dict[str, Any]:
        if offset < 0:
            raise ValidationFailed(["offset must not be negative"])
        async with self.store.session_factory() as session:
            generation = await session.scalar(
                sa.select(FieldIndexGeneration).where(FieldIndexGeneration.active)
            )
            invalid = []
            total = 0
            if generation:
                statement = (
                    sa.select(
                        NoteFieldValue.note_id,
                        Note.path,
                        NoteFieldValue.field,
                        NoteFieldValue.invalid_reason,
                    )
                    .join(Note, Note.id == NoteFieldValue.note_id)
                    .where(
                        NoteFieldValue.generation == generation.id,
                        NoteFieldValue.invalid_reason.is_not(None),
                    )
                )
                total = int(
                    await session.scalar(
                        sa.select(sa.func.count()).select_from(statement.subquery())
                    )
                    or 0
                )
                invalid = [
                    dict(row._mapping)
                    for row in (
                        await session.execute(
                            statement.order_by(NoteFieldValue.note_id, NoteFieldValue.field)
                            .offset(offset)
                            .limit(100)
                        )
                    ).all()
                ]
            return {
                "job": self.receipt(),
                "active_generation": generation.id if generation else None,
                "fields": [
                    dict(**key, ready=key in (generation.definitions if generation else []))
                    for key in definitions(self.store)
                ],
                "invalid_count": total,
                "invalid": invalid,
                "next_offset": offset + 100 if offset + 100 < total else None,
                "current_schema_revision": self.store.control.schema_document().revision,
            }

    def pending_request(self) -> str | None:
        try:
            return self.request_path.read_text().strip() or None
        except FileNotFoundError:
            return None

    def acknowledge_request(self, request: str | None) -> None:
        # No await between comparison and acknowledgment. A newer request belongs
        # to the next pass, even if this pass failed or merely checked readiness.
        if request is not None and self.pending_request() == request:
            atomic_write_text(self.request_path, "", mode=0o600)

    def request_rebuild(self) -> dict[str, Any]:
        atomic_write_text(self.request_path, new_id(), mode=0o600)
        if not self.lock.locked():
            self.write({"state": "queued", "job_id": self.target_id(), "processed": 0})
        return self.receipt()

    async def refresh(self, *, force: bool = False) -> None:
        async with self.lock:
            keys = definitions(self.store)
            target = filter_digest(keys)
            previous = self.receipt()
            request = self.pending_request()
            forced = (
                force
                or request is not None
                or (self.path.exists() and previous.get("state") in {"queued", "running"})
            )
            if (
                not forced
                and previous.get("job_id") == target
                and previous.get("state") == "failed"
            ):
                return
            record: dict[str, Any] = {
                "job_id": target,
                "state": "running",
                "processed": 0,
                "started_at": datetime.now(UTC).isoformat(),
            }
            try:
                async with self.store.session_factory() as session, session.begin():
                    # A bounded mirror-write barrier prevents a snapshot from missing a
                    # concurrent mirror update. Reads retain the old committed generation.
                    await session.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
                    await session.execute(sa.text("SET LOCAL statement_timeout = '30s'"))
                    active = await session.scalar(
                        sa.select(FieldIndexGeneration).where(FieldIndexGeneration.active)
                    )
                    if active and active.definitions == keys and not forced:
                        if previous.get("state") != "succeeded" or previous.get("job_id") != target:
                            self.write(dict(record, state="succeeded"))
                        return
                    await session.execute(sa.text("LOCK TABLE notes IN SHARE MODE"))
                    self.write(record)
                    generation = FieldIndexGeneration(
                        id=new_id(),
                        schema_revision=self.store.control.schema_document().revision,
                        definitions=keys,
                        active=False,
                        created_at=datetime.now(UTC),
                    )
                    session.add(generation)
                    await session.flush()
                    after = ""
                    async with asyncio.timeout(30):
                        while True:
                            rows = (
                                await session.execute(
                                    sa.select(Note.id)
                                    .where(Note.id > after)
                                    .order_by(Note.id)
                                    .limit(100)
                                )
                            ).all()
                            if not rows:
                                break
                            await session.execute(
                                sa.text(
                                    "SELECT coppermind_index_fields(:generation, id, frontmatter) "
                                    "FROM notes WHERE id > :after AND id <= :last"
                                ),
                                {"generation": generation.id, "after": after, "last": rows[-1].id},
                            )
                            after = rows[-1].id
                            record["processed"] += len(rows)
                            self.write(record)
                        if definitions(self.store) != keys:
                            raise ValueError("schema changed during rebuild")
                        if active:
                            await session.delete(active)
                            await session.flush()
                        generation.active = True
                record.update(state="succeeded", completed_at=datetime.now(UTC).isoformat())
                self.write(record)
                self.acknowledge_request(request)
            except asyncio.CancelledError:
                raise
            except Exception:
                record.update(
                    state="failed",
                    error=(
                        "Rebuild did not finish; the previous generation remains available. "
                        "Retry after checking database access and schema changes."
                    ),
                )
                self.write(record)
                self.acknowledge_request(request)

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(10)
