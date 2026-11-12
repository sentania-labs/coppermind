"""Durable calendar import promotion independent of the PostgreSQL mirror.

The import original and receipt are retained before an atomic catalog replacement.
A pending receipt can be retried after a crash; a catalog records completed imports,
so a crash between promotion and receipt completion cannot apply an export twice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import ijson
import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field

from coppermind.atomicio import atomic_write_bytes, atomic_write_text
from coppermind.db.calendar import calendar_events as rows
from coppermind.db.calendar import calendar_evidence as evidence_rows
from coppermind.db.calendar import calendar_state as mirror_state
from coppermind.logging import get_logger
from coppermind.settings import LimitSettings
from coppermind_store import calendar_mirror as mirror
from coppermind_store.calendar_import import (
    CALENDAR_ID,
    MAX_UPLOAD,
    CalendarEvent,
    CalendarImportError,
    CalendarProblem,
    CalendarTransientError,
    EventProvenance,
    merge_with_problems,
)
from coppermind_store.calendar_worker import parse_bounded

log = logging.getLogger(__name__)


class ImportReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    calendar_id: str
    filename: str
    content_hash: str
    state: Literal["pending", "succeeded", "failed"] = "pending"
    event_count: int = 0
    # An equal-revision conflict keeps the stored copy: `kept_count` events were left
    # as stored, `conflicts` carries one plain-words problem per such event.
    kept_count: int = 0
    conflicts: list[CalendarProblem] = Field(default_factory=list)
    error: str = ""
    received_at: str
    imported_at: str = ""
    # The ceiling this archived buffer was measured against: MAX_UPLOAD for an original
    # drop, MAX_EXPANDED for a calendar extracted from a ZIP. Recorded so a retried
    # receipt reuses the ceiling its bytes were accepted under.
    limit: int = MAX_UPLOAD
    legacy_calendar: str = ""
    export_filename: str = ""
    original_entry: str = ""
    dismissed: bool = False
    retryable: bool = False


class ContactEvidence(BaseModel):
    event_id: str
    address: str
    name: str
    role: str
    participation: str
    source_import: str = ""


class CalendarContact(BaseModel):
    id: str
    evidence: list[ContactEvidence] = Field(default_factory=list)
    evidence_count: int = 0


class CalendarArchive:
    """One instance owned by the single store writer; no vault writes."""

    def __init__(
        self,
        state_dir: Path,
        database_url: str | None = None,
        limits: Callable[[], LimitSettings] = LimitSettings,
    ) -> None:
        self.root = state_dir / "calendars"
        self._lock = threading.RLock()
        self.limits = limits
        self.root.mkdir(parents=True, exist_ok=True)
        # Standalone consumers have no PostgreSQL connection. Production always
        # supplies the store's PostgreSQL URL; the filesystem is truth in both.
        self.engine = sa.create_engine(
            database_url or f"sqlite:///{self.root / 'mirror.sqlite3'}",
            pool_pre_ping=True,
        )
        if self.engine.dialect.name == "sqlite":
            mirror_state.metadata.create_all(
                self.engine,
                tables=[
                    rows,
                    mirror.evidence,
                    mirror_state,
                ],
            )

    def catalog(self) -> mirror.CatalogView:
        path = self.root / "catalog.json"
        # Readers can serve the last committed mirror while an import parses and
        # writes its transaction. Only a stale mirror needs the writer lock.
        with self.engine.connect() as connection:
            if mirror.get_state(connection, "signature") == mirror.signature(path):
                imports = json.loads(mirror.get_state(connection, "imports", "[]"))
                return mirror.CatalogView(self.engine, imports)
        with self._lock, self.engine.begin() as connection:
            if mirror.get_state(connection, "signature") != mirror.signature(path):
                logger = get_logger("coppermind-store")
                logger.info("calendar_mirror_rebuild_started")
                mirror.rebuild(connection, path)
                logger.info("calendar_mirror_rebuild_completed")
            imports = json.loads(mirror.get_state(connection, "imports", "[]"))
        return mirror.CatalogView(self.engine, imports)

    def receipts(self) -> list[ImportReceipt]:
        return [
            ImportReceipt.model_validate_json(path.read_bytes())
            for path in sorted((self.root / "imports").glob("*/receipt.json"))
        ]

    def _save_receipt(self, receipt: ImportReceipt) -> None:
        atomic_write_text(
            self.root / "imports" / receipt.id / "receipt.json",
            receipt.model_dump_json(),
            mode=0o600,
        )

    def submit(
        self,
        calendar_id: str,
        filename: str,
        data: bytes,
        limit: int = MAX_UPLOAD,
        *,
        export_filename: str = "",
    ) -> ImportReceipt:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", calendar_id):
            raise CalendarImportError("calendar identity must be a short lowercase slug")
        if len(data) > limit:
            raise CalendarImportError(f"calendar upload exceeds {limit / 1_000_000:g} MB")
        # The filename is display metadata, never a local filesystem destination.
        if not filename or len(filename) > 255 or any(ord(c) < 32 for c in filename):
            raise CalendarImportError("invalid upload filename")
        suffix = Path(filename).suffix.lower()
        if suffix not in {".ics", ".zip"}:
            raise CalendarImportError("expected an ICS or ZIP file")
        digest = hashlib.sha256(data).hexdigest()
        identity = hashlib.sha256(f"{suffix}\0{digest}".encode()).hexdigest()
        with self._lock:
            existing = next((r for r in self.receipts() if r.content_hash == digest), None)
            if existing is not None:
                if existing.dismissed or (existing.state == "failed" and existing.retryable):
                    existing.dismissed = False
                    if existing.state == "failed" and existing.retryable:
                        existing.state, existing.error = "pending", ""
                    self._save_receipt(existing)
                return existing
            receipt_path = self.root / "imports" / identity / "receipt.json"
            if receipt_path.exists():
                return ImportReceipt.model_validate_json(receipt_path.read_bytes())
            receipt = ImportReceipt(
                id=identity,
                calendar_id=CALENDAR_ID,
                filename=filename,
                export_filename=export_filename or filename,
                content_hash=digest,
                received_at=datetime.now(UTC).isoformat(),
                limit=limit,
            )
            original = receipt_path.parent / "original"
            if original.exists() and hashlib.sha256(original.read_bytes()).hexdigest() != digest:
                raise CalendarImportError("archived original failed its integrity check")
            if not original.exists():
                atomic_write_bytes(original, data, mode=0o600)
            self._save_receipt(receipt)
            return receipt

    def dismiss(self, receipt_id: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}", receipt_id):
            raise CalendarImportError("invalid import identity")
        with self._lock:
            path = self.root / "imports" / receipt_id / "receipt.json"
            receipt = ImportReceipt.model_validate_json(path.read_bytes())
            if receipt.state == "failed" and not receipt.dismissed:
                receipt.dismissed = True
                self._save_receipt(receipt)

    def process(self, receipt_id: str) -> ImportReceipt:
        if not re.fullmatch(r"[a-f0-9]{64}", receipt_id):
            raise CalendarImportError("invalid import identity")
        with self._lock:
            receipt_path = self.root / "imports" / receipt_id / "receipt.json"
            receipt = ImportReceipt.model_validate_json(receipt_path.read_bytes())
            catalog = self.catalog()
            if receipt.id in catalog.imports:
                receipt.state = "succeeded"
                receipt.error = ""
                receipt.imported_at = receipt.imported_at or receipt.received_at
                self._save_receipt(receipt)
                return receipt
            original = (receipt_path.parent / "original").read_bytes()
            if hashlib.sha256(original).hexdigest() != receipt.content_hash:
                raise CalendarImportError("archived original failed its integrity check")
            try:
                limits = self.limits()
                events = parse_bounded(
                    CALENDAR_ID,
                    receipt.filename,
                    original,
                    receipt.limit,
                    max_events=limits.calendar_max_events,
                    memory_mib=limits.calendar_import_memory_mib,
                )
                for event in events:
                    event.provenance = [
                        EventProvenance(
                            source_id=receipt.id,
                            filename=receipt.export_filename or receipt.filename,
                            entry=receipt.original_entry or entry,
                            imported_at=receipt.received_at,
                            legacy_calendar=receipt.legacy_calendar
                            or (receipt.calendar_id if receipt.calendar_id != CALENDAR_ID else ""),
                        )
                        for entry in event.source_entries or [event.source_entry]
                    ]
                with self.engine.begin() as connection:
                    count = connection.execute(
                        sa.select(sa.func.count()).select_from(rows)
                    ).scalar_one()
                    stored = {
                        event.identity: CalendarEvent.model_validate_json(value)
                        for event in events
                        if (
                            value := connection.execute(
                                sa.select(rows.c.record).where(rows.c.id == event.identity)
                            ).scalar()
                        )
                        is not None
                    }
                    # Every event merges in one pass: an equal-revision conflict keeps
                    # the stored copy and is reported per event, the rest still lands.
                    merged, problems = merge_with_problems(stored, events)
                    written = 0
                    for event in events:
                        previous = stored.get(event.identity)
                        if previous is None:
                            count += 1
                            if count > limits.calendar_max_events:
                                raise CalendarImportError(
                                    "calendar event budget exceeded: "
                                    f"limits.calendar_max_events={limits.calendar_max_events}"
                                )
                        winner = merged[event.identity]
                        if winner != previous:
                            written += 1
                            mirror.put_event(connection, event.identity, winner)
                        for person in event.people:
                            if person.contact_id is not None:
                                evidence = ContactEvidence(
                                    event_id=event.identity,
                                    address=person.address,
                                    name=person.name,
                                    role=person.role,
                                    participation=person.participation,
                                    source_import=receipt.id,
                                )
                                mirror.add_evidence(
                                    connection, person.contact_id, evidence.model_dump()
                                )
                    mirror.drop_superseded_conflicts(connection)
                    receipt.event_count = written
                    receipt.kept_count = len(problems)
                    # Each disagreeing incoming version is recorded once per event, in the
                    # shape the acknowledge path already keeps on the mirror.
                    receipt.conflicts = _retain_conflicts(connection, problems, events)
                    # Persist the count before promotion so crash recovery can finish the receipt.
                    self._save_receipt(receipt)
                    # The atomic filesystem promotion precedes the database commit.
                    # A crash in between changes its signature and triggers a rebuild.
                    mirror.write_catalog(
                        connection, self.root / "catalog.json", [*catalog.imports, receipt.id]
                    )
            except CalendarImportError as exc:
                receipt.state = "failed"
                receipt.retryable = isinstance(exc, CalendarTransientError)
                receipt.error = (
                    f"{str(exc).rstrip('.')}. Drop the export again to retry."
                    if receipt.retryable
                    else str(exc)
                )
                get_logger("coppermind-store").warning(
                    "calendar_import_failed",
                    receipt_id=receipt.id,
                    reason=receipt.error,
                )
                receipt.dismissed = False
                self._save_receipt(receipt)
                return receipt
            receipt.state = "succeeded"
            receipt.error = ""
            receipt.imported_at = receipt.imported_at or datetime.now(UTC).isoformat()
            self._save_receipt(receipt)
            return receipt

    def dismiss_conflict(self, event_id: str, version: str) -> None:
        with self._lock:
            catalog = self.catalog()
            with self.engine.begin() as connection:
                mirror.put_conflicts(
                    connection,
                    [
                        event.model_copy(update={"acknowledged": True})
                        if (event.identity, event.semantic_hash) == (event_id, version)
                        else event
                        for event in mirror.get_conflicts(connection)
                    ],
                )
                mirror.write_catalog(connection, self.root / "catalog.json", list(catalog.imports))

    def migrate(
        self, legacy_names: dict[str, str], origins: dict[str, tuple[str, str]] | None = None
    ) -> None:
        """One atomic catalog promotion, restart safe; originals/receipts never removed."""
        with self._lock:
            path = self.root / "catalog.json"
            if not path.exists():
                return
            with path.open("rb") as handle:
                if next(ijson.items(handle, "schema_version"), 1) == 2:
                    return
            backup = self.root / "before-one-calendar.json"
            if not backup.exists():
                staged = backup.with_name(".before-one-calendar.tmp")
                shutil.copyfile(path, staged)
                staged.chmod(0o600)
                staged.replace(backup)
            old = self.catalog()
            witnesses: dict[str, list[EventProvenance]] = {}
            receipts = self.receipts()
            receipt_backup = self.root / "before-one-calendar-imports.json"
            if not receipt_backup.exists():
                atomic_write_text(
                    receipt_backup,
                    json.dumps(
                        {
                            receipt.id: json.loads(
                                (self.root / "imports" / receipt.id / "receipt.json").read_bytes()
                            )
                            for receipt in receipts
                        }
                    ),
                    mode=0o600,
                )
            limits = self.limits()
            for receipt in receipts:
                receipt.legacy_calendar = legacy_names.get(receipt.calendar_id, receipt.calendar_id)
                if origins and receipt.id in origins:
                    receipt.export_filename, receipt.original_entry = origins[receipt.id]
                self._save_receipt(receipt)
                if receipt.id not in old.imports:
                    continue
                try:
                    original = (self.root / "imports" / receipt.id / "original").read_bytes()
                    if hashlib.sha256(original).hexdigest() != receipt.content_hash:
                        raise CalendarImportError("archived original failed its integrity check")
                    parsed = parse_bounded(
                        receipt.calendar_id,
                        receipt.filename,
                        original,
                        receipt.limit,
                        max_events=limits.calendar_max_events,
                        memory_mib=limits.calendar_import_memory_mib,
                    )
                except (CalendarImportError, OSError) as exc:
                    log.warning(
                        "Calendar migration kept existing provenance for import %s: %s",
                        receipt.id,
                        exc,
                    )
                    continue
                for event in parsed:
                    witnesses.setdefault(event.identity, []).extend(
                        EventProvenance(
                            source_id=receipt.id,
                            filename=receipt.export_filename or receipt.filename,
                            entry=receipt.original_entry or entry,
                            imported_at=receipt.received_at,
                            legacy_calendar=legacy_names.get(
                                receipt.calendar_id, receipt.calendar_id
                            ),
                        )
                        for entry in event.source_entries or [event.source_entry]
                    )
            # Stage the former rows and evidence on disk so the rewrite never holds the
            # catalog in memory. Each former record is converted and merged into the one
            # calendar's rows in a single transaction that ends with the file promotion.
            aliases: dict[str, str] = {}
            conflicts: list[CalendarEvent] = []
            with tempfile.TemporaryDirectory(dir=self.root, prefix=".migrate-") as scratch:
                former = Path(scratch) / "events.jsonl"
                with former.open("w") as handle:
                    for old_id, event in old.events.items():
                        handle.write(json.dumps([old_id, event.model_dump_json()]) + "\n")
                with self.engine.begin() as connection:
                    staged_evidence = Path(scratch) / "evidence.jsonl"
                    with staged_evidence.open("w") as handle:
                        for row in mirror.stream(
                            connection,
                            sa.select(evidence_rows.c.contact_id, evidence_rows.c.record),
                        ):
                            handle.write(json.dumps([row.contact_id, row.record]) + "\n")
                    connection.execute(rows.delete())
                    connection.execute(evidence_rows.delete())
                    with former.open() as handle:
                        for line in handle:
                            old_id, encoded = json.loads(line)
                            event = CalendarEvent.model_validate_json(encoded)
                            previous_calendar = event.calendar_id
                            event.provenance = witnesses.get(old_id, event.provenance)
                            if not event.provenance:
                                event.provenance = [
                                    EventProvenance(
                                        source_id=previous_calendar,
                                        filename=event.source_entry,
                                        entry=event.source_entry,
                                        imported_at="",
                                        legacy_calendar=legacy_names.get(
                                            previous_calendar, previous_calendar
                                        ),
                                    )
                                ]
                            event.calendar_id = CALENDAR_ID
                            aliases[old_id] = event.identity
                            value = connection.execute(
                                sa.select(rows.c.record).where(rows.c.id == event.identity)
                            ).scalar()
                            current = CalendarEvent.model_validate_json(value) if value else None
                            merged, problems = merge_with_problems(
                                {event.identity: current} if current else {}, [event]
                            )
                            if problems:
                                # Migration keeps the former shape: the disagreeing
                                # record itself is retained in the conflict list.
                                conflicts.append(event)
                                continue
                            mirror.put_event(connection, event.identity, merged[event.identity])
                    with staged_evidence.open() as handle:
                        for line in handle:
                            contact_id, encoded = json.loads(line)
                            record = json.loads(encoded)
                            record["event_id"] = aliases.get(record["event_id"], record["event_id"])
                            mirror.add_evidence(connection, contact_id, record)
                    mirror.put_conflicts(connection, conflicts)
                    mirror.drop_superseded_conflicts(connection)
                    mirror.put_state(connection, "aliases", json.dumps(aliases))
                    mirror.write_catalog(connection, path, list(old.imports))

    def export_manifest(self) -> dict[str, object]:
        """A read-only summary; originals and receipt paths stay server-side."""
        catalog = self.catalog()
        return {
            "events": len(catalog.events),
            "contacts": len(catalog.contacts()),
            "imports": [json.loads(receipt.model_dump_json()) for receipt in self.receipts()],
        }


def _retain_conflicts(
    connection: Any, problems: list[CalendarProblem], events: list[CalendarEvent]
) -> list[CalendarProblem]:
    """Record each disagreeing incoming version once and report only new ones.

    A record is keyed by ``(identity, semantic_hash)`` of the incoming event. The stored
    copy stays in the mirror's events; dismissing marks the record acknowledged instead of
    deleting it, so the same export imported again adds nothing and stays quiet.
    """
    incoming = {event.identity: event for event in events}
    held = mirror.get_conflicts(connection)
    known = {(event.identity, event.semantic_hash): event for event in held}
    retained: list[CalendarProblem] = []
    for problem in problems:
        key = (problem.event_id, problem.version)
        if key not in known:
            known[key] = incoming[problem.event_id]
        elif known[key].acknowledged:
            continue
        retained.append(problem)
    if len(known) != len(held):
        mirror.put_conflicts(connection, list(known.values()))
    return retained
