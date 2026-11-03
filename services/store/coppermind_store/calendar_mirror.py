"""Bounded query/read/write access to the rebuildable calendar catalog.

No catalog-sized JSON string, model graph, or contact list crosses a request.
PostgreSQL is the live query copy. A local SQLite mirror is used by standalone
archive consumers (including unit tests); both rebuild from the same durable file.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, overload

import ijson
import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from coppermind.atomicio import commit_staged
from coppermind.db.calendar import calendar_events as events
from coppermind.db.calendar import calendar_evidence as evidence
from coppermind.db.calendar import calendar_state as state
from coppermind_store.calendar_import import CalendarEvent, CalendarImportError

if TYPE_CHECKING:
    from coppermind_store.calendar_archive import CalendarContact


def signature(path: Path) -> str:
    if not path.exists():
        return "absent"
    info = path.stat()
    return f"{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"


def stream(connection: Connection, query: Any) -> Any:
    return connection.execute(query.execution_options(yield_per=100))


def put_state(connection: Connection, key: str, value: str) -> None:
    connection.execute(state.delete().where(state.c.key == key))
    connection.execute(state.insert().values(key=key, value=value))


def get_state(connection: Connection, key: str, default: str = "") -> str:
    return (
        connection.execute(sa.select(state.c.value).where(state.c.key == key)).scalar() or default
    )


def add_evidence(connection: Connection, contact_id: str, record: dict[str, Any]) -> None:
    encoded = json.dumps(record, sort_keys=True)
    identity = hashlib.sha256((contact_id + "\0" + encoded).encode()).hexdigest()
    # A primary-key lookup replaces quadratic Python model comparisons.
    if (
        connection.execute(sa.select(evidence.c.id).where(evidence.c.id == identity)).first()
        is None
    ):
        connection.execute(
            evidence.insert().values(id=identity, contact_id=contact_id, record=encoded)
        )


def put_event(connection: Connection, identity: str, event: CalendarEvent) -> None:
    connection.execute(events.delete().where(events.c.id == identity))
    connection.execute(
        events.insert().values(
            id=identity,
            calendar_id=event.calendar_id,
            uid=event.uid,
            semantic_hash=event.semantic_hash,
            record=event.model_dump_json(),
        )
    )


def rebuild(connection: Connection, path: Path) -> None:
    """A crash rolls back the mirror; a later reader safely retries the rebuild."""
    connection.execute(events.delete())
    connection.execute(evidence.delete())
    imports: list[str] = []
    if path.exists():
        with path.open("rb") as handle:
            version = next(ijson.items(handle, "schema_version"), 1)
        if version != 1:
            raise CalendarImportError("unsupported calendar catalog schema version")
        with path.open("rb") as handle:
            for identity, event_record in ijson.kvitems(handle, "events"):
                put_event(connection, identity, CalendarEvent.model_validate(event_record))
        with path.open("rb") as handle:
            imports = list(ijson.items(handle, "imports.item"))
        # Build one evidence record at a time, even when a single contact has
        # participated in the entire calendar. Avoid kvitems(contact_records).
        contact = ""
        record: dict[str, Any] | None = None
        field = ""
        found = False
        with path.open("rb") as handle:
            for prefix, kind, value in ijson.parse(handle):
                if prefix == "contact_records" and kind == "map_key":
                    contact = value
                    found = True
                elif prefix.endswith(".evidence.item"):
                    if kind == "start_map":
                        record = {}
                    elif kind == "map_key":
                        field = value
                    elif kind == "end_map" and record is not None:
                        add_evidence(connection, contact, record)
                        record = None
                elif record is not None and kind in {"string", "number", "boolean", "null"}:
                    record[field] = value
        if not found:
            for row in stream(connection, sa.select(events.c.record)):
                event = CalendarEvent.model_validate_json(row.record)
                for person in event.people:
                    if person.contact_id:
                        add_evidence(
                            connection,
                            person.contact_id,
                            {
                                "event_id": event.identity,
                                "address": person.address,
                                "name": person.name,
                                "role": person.role,
                                "participation": person.participation,
                                "source_import": "",
                            },
                        )
    put_state(connection, "imports", json.dumps(imports))
    put_state(connection, "signature", signature(path))


def write_catalog(connection: Connection, path: Path, imports: list[str]) -> None:
    """Stream the mirror to an atomic, backwards-readable filesystem snapshot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".catalog.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write('{"schema_version":1,"events":{')
            separator = ""
            for row in stream(
                connection, sa.select(events.c.id, events.c.record).order_by(events.c.id)
            ):
                handle.write(separator + json.dumps(row.id) + ":" + row.record)
                separator = ","
            handle.write('},"imports":' + json.dumps(imports) + ',"contact_records":{')
            previous = None
            separator = ""
            for row in stream(
                connection,
                sa.select(evidence.c.contact_id, evidence.c.record).order_by(
                    evidence.c.contact_id, evidence.c.id
                ),
            ):
                if row.contact_id != previous:
                    if previous is not None:
                        handle.write("]}")
                    handle.write(
                        separator
                        + json.dumps(row.contact_id)
                        + ':{"id":'
                        + json.dumps(row.contact_id)
                        + ',"evidence":['
                    )
                    previous = row.contact_id
                    separator = ","
                    item_separator = ""
                handle.write(item_separator + row.record)
                item_separator = ","
            if previous is not None:
                handle.write("]}")
            handle.write("}}")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        commit_staged(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    put_state(connection, "imports", json.dumps(imports))
    put_state(connection, "signature", signature(path))


class EventRows(Mapping[str, CalendarEvent]):
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def __len__(self) -> int:
        with self.engine.connect() as connection:
            return connection.execute(sa.select(sa.func.count()).select_from(events)).scalar_one()

    def __getitem__(self, key: str) -> CalendarEvent:
        with self.engine.connect() as connection:
            value = connection.execute(
                sa.select(events.c.record).where(events.c.id == key)
            ).scalar()
        if value is None:
            raise KeyError(key)
        return CalendarEvent.model_validate_json(value)

    def __iter__(self) -> Iterator[str]:
        with self.engine.connect() as connection:
            yield from stream(connection, sa.select(events.c.id).order_by(events.c.id)).scalars()

    def values(self) -> Iterator[CalendarEvent]:  # type: ignore[override]
        yield from self.page(None)

    def page(
        self, limit: int | None = 100, offset: int = 0, calendar_id: str | None = None
    ) -> Iterator[CalendarEvent]:
        query = sa.select(events.c.record)
        if calendar_id is not None:
            query = query.where(events.c.calendar_id == calendar_id).order_by(
                events.c.uid, events.c.id
            )
        else:
            query = query.order_by(events.c.id)
        if limit is not None:
            query = query.limit(limit)
        with self.engine.connect() as connection:
            for value in stream(connection, query.offset(offset)).scalars():
                yield CalendarEvent.model_validate_json(value)

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        with self.engine.connect() as connection:
            for row in stream(
                connection, sa.select(events.c.id, events.c.semantic_hash).order_by(events.c.id)
            ):
                digest.update(f"{row.id}:{row.semantic_hash}\n".encode())
        return digest.hexdigest()

    def context_fingerprint(self, contacts: Any) -> str:
        """Stream the former sorted-pairs JSON hash without changing job evidence."""
        digest = hashlib.sha256()
        digest.update(b'{"calendar": [')
        separator = b""
        with self.engine.connect() as connection:
            for row in stream(
                connection, sa.select(events.c.id, events.c.semantic_hash).order_by(events.c.id)
            ):
                digest.update(separator)
                digest.update(json.dumps([row.id, row.semantic_hash]).encode())
                separator = b", "
        digest.update(b'], "contacts": ')
        digest.update(json.dumps(contacts, sort_keys=True, default=str).encode())
        digest.update(b"}")
        return digest.hexdigest()


class ContactRows(Sequence["CalendarContact"]):
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def __len__(self) -> int:
        with self.engine.connect() as connection:
            return connection.execute(
                sa.select(sa.func.count(sa.distinct(evidence.c.contact_id)))
            ).scalar_one()

    @overload
    def __getitem__(self, index: int) -> CalendarContact: ...
    @overload
    def __getitem__(self, index: slice) -> list[CalendarContact]: ...
    def __getitem__(self, index: int | slice) -> CalendarContact | list[CalendarContact]:
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            return list(self.page(stop - start, start))[::step]
        if index < 0:
            index += len(self)
        result = list(self.page(1, index))
        if index < 0 or not result:
            raise IndexError(index)
        return result[0]

    def __iter__(self) -> Iterator[CalendarContact]:
        yield from self.page(None)

    def page(self, limit: int | None = 100, offset: int = 0) -> Iterator[CalendarContact]:
        from coppermind_store.calendar_archive import CalendarContact, ContactEvidence

        # A page bounds IDs; streaming callers obtain subsequent pages separately.
        if limit is None:
            position = offset
            while True:
                batch = list(self.page(100, position))
                if not batch:
                    return
                yield from batch
                position += len(batch)
            return

        ids = (
            sa.select(evidence.c.contact_id)
            .distinct()
            .order_by(evidence.c.contact_id)
            .limit(limit)
            .offset(offset)
            .subquery()
        )
        # Rank IDs, not the evidence payloads. The join fetches at most 100
        # records per selected contact while the window counts its full history.
        # This stays one round trip for the entire contact page.
        ranked = (
            sa.select(
                evidence.c.id.label("evidence_id"),
                evidence.c.contact_id,
                sa.func.count().over(partition_by=evidence.c.contact_id).label("total"),
                sa.func.row_number()
                .over(partition_by=evidence.c.contact_id, order_by=evidence.c.id)
                .label("position"),
            )
            .where(evidence.c.contact_id.in_(sa.select(ids.c.contact_id)))
            .subquery()
        )
        query = (
            sa.select(ranked.c.contact_id, ranked.c.total, evidence.c.record)
            .join_from(ranked, evidence, evidence.c.id == ranked.c.evidence_id)
            .where(ranked.c.position <= 100)
            .order_by(ranked.c.contact_id, ranked.c.position)
        )
        contact = None
        with self.engine.connect() as connection:
            for row in stream(connection, query):
                if contact is None or contact.id != row.contact_id:
                    if contact is not None:
                        yield contact
                    contact = CalendarContact(id=row.contact_id, evidence_count=row.total)
                contact.evidence.append(ContactEvidence.model_validate_json(row.record))
        if contact is not None:
            yield contact

    def has(self, identity: str) -> bool:
        with self.engine.connect() as connection:
            return (
                connection.execute(
                    sa.select(evidence.c.id).where(evidence.c.contact_id == identity).limit(1)
                ).first()
                is not None
            )

    def addresses(self, identity: str) -> set[str]:
        addresses = set()
        with self.engine.connect() as connection:
            for value in stream(
                connection, sa.select(evidence.c.record).where(evidence.c.contact_id == identity)
            ).scalars():
                addresses.add(json.loads(value)["address"].partition(":")[2])
        return addresses

    def contact(self, identity: str) -> CalendarContact:
        from coppermind_store.calendar_archive import CalendarContact, ContactEvidence

        with self.engine.connect() as connection:
            count = connection.execute(
                sa.select(sa.func.count())
                .select_from(evidence)
                .where(evidence.c.contact_id == identity)
            ).scalar_one()
            records = connection.execute(
                sa.select(evidence.c.record)
                .where(evidence.c.contact_id == identity)
                .order_by(evidence.c.id)
                .limit(100)
            )
            return CalendarContact(
                id=identity,
                evidence_count=count,
                evidence=[
                    ContactEvidence.model_validate_json(value) for value in records.scalars()
                ],
            )


class CatalogView:
    """A lightweight query facade with the archive's former read interface."""

    def __init__(self, engine: Engine, imports: list[str]) -> None:
        self.engine = engine
        self.events = EventRows(engine)
        self.imports = imports

    def contacts(self) -> ContactRows:
        return ContactRows(self.engine)
