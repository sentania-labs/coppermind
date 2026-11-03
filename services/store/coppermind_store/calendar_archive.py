"""Durable calendar import promotion independent of the PostgreSQL mirror.

The import original and receipt are retained before an atomic catalog replacement.
A pending receipt can be retried after a crash; a catalog records completed imports,
so a crash between promotion and receipt completion cannot apply an export twice.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field

from coppermind.atomicio import atomic_write_bytes, atomic_write_text
from coppermind.db.calendar import calendar_events as rows
from coppermind.db.calendar import calendar_state as mirror_state
from coppermind.logging import get_logger
from coppermind.settings import LimitSettings
from coppermind_store import calendar_mirror as mirror
from coppermind_store.calendar_import import (
    MAX_UPLOAD,
    CalendarEvent,
    CalendarImportError,
)
from coppermind_store.calendar_worker import parse_bounded


class ImportReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    calendar_id: str
    filename: str
    content_hash: str
    state: Literal["pending", "succeeded", "failed"] = "pending"
    event_count: int = 0
    error: str = ""
    received_at: str
    # The ceiling this archived buffer was measured against: MAX_UPLOAD for an original
    # drop, MAX_EXPANDED for a calendar extracted from a ZIP. Recorded so a retried
    # receipt reuses the ceiling its bytes were accepted under.
    limit: int = MAX_UPLOAD


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
        self, calendar_id: str, filename: str, data: bytes, limit: int = MAX_UPLOAD
    ) -> ImportReceipt:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", calendar_id):
            raise CalendarImportError("calendar identity must be a short lowercase slug")
        if len(data) > limit:
            raise CalendarImportError(f"calendar upload exceeds {limit // (1024 * 1024)} MiB")
        # The filename is display metadata, never a local filesystem destination.
        if not filename or len(filename) > 255 or any(ord(c) < 32 for c in filename):
            raise CalendarImportError("invalid upload filename")
        suffix = Path(filename).suffix.lower()
        if suffix not in {".ics", ".zip"}:
            raise CalendarImportError("expected an ICS or ZIP file")
        digest = hashlib.sha256(data).hexdigest()
        identity = hashlib.sha256(f"{calendar_id}\0{suffix}\0{digest}".encode()).hexdigest()
        with self._lock:
            receipt_path = self.root / "imports" / identity / "receipt.json"
            if receipt_path.exists():
                return ImportReceipt.model_validate_json(receipt_path.read_bytes())
            receipt = ImportReceipt(
                id=identity,
                calendar_id=calendar_id,
                filename=filename,
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
                self._save_receipt(receipt)
                return receipt
            original = (receipt_path.parent / "original").read_bytes()
            if hashlib.sha256(original).hexdigest() != receipt.content_hash:
                raise CalendarImportError("archived original failed its integrity check")
            try:
                limits = self.limits()
                events = parse_bounded(
                    receipt.calendar_id,
                    receipt.filename,
                    original,
                    receipt.limit,
                    max_events=limits.calendar_max_events,
                    memory_mib=limits.calendar_import_memory_mib,
                )
                with self.engine.begin() as connection:
                    count = connection.execute(
                        sa.select(sa.func.count()).select_from(rows)
                    ).scalar_one()
                    for event in events:
                        value = connection.execute(
                            sa.select(rows.c.record).where(rows.c.id == event.identity)
                        ).scalar()
                        previous = CalendarEvent.model_validate_json(value) if value else None
                        if previous is None:
                            count += 1
                            if count > limits.calendar_max_events:
                                raise CalendarImportError(
                                    "calendar event budget exceeded: "
                                    f"limits.calendar_max_events={limits.calendar_max_events}"
                                )
                        if previous is None or event.revision > previous.revision:
                            mirror.put_event(connection, event.identity, event)
                        elif (
                            event.revision == previous.revision
                            and event.semantic_hash != previous.semantic_hash
                        ):
                            raise CalendarImportError(
                                "event revision conflicts with the stored calendar"
                            )
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
                    receipt.event_count = len(events)
                    self._save_receipt(receipt)
                    # The atomic filesystem promotion precedes the database commit.
                    # A crash in between changes its signature and triggers a rebuild.
                    mirror.write_catalog(
                        connection, self.root / "catalog.json", [*catalog.imports, receipt.id]
                    )
            except CalendarImportError as exc:
                receipt.state = "failed"
                receipt.error = str(exc)
                get_logger("coppermind-store").warning(
                    "calendar_import_failed",
                    receipt_id=receipt.id,
                    reason=receipt.error,
                )
                self._save_receipt(receipt)
                return receipt
            receipt.state = "succeeded"
            receipt.error = ""
            self._save_receipt(receipt)
            return receipt

    def export_manifest(self) -> dict[str, object]:
        """A read-only summary; originals and receipt paths stay server-side."""
        catalog = self.catalog()
        return {
            "events": len(catalog.events),
            "contacts": len(catalog.contacts()),
            "imports": [json.loads(receipt.model_dump_json()) for receipt in self.receipts()],
        }
