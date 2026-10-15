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
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coppermind.atomicio import atomic_write_bytes, atomic_write_text
from coppermind_store.calendar_import import (
    MAX_UPLOAD,
    CalendarEvent,
    CalendarImportError,
    merge_events,
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


class CalendarCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    events: dict[str, CalendarEvent] = Field(default_factory=dict)
    imports: list[str] = Field(default_factory=list)
    contact_records: dict[str, CalendarContact] = Field(default_factory=dict)

    def contacts(self) -> list[CalendarContact]:
        if self.contact_records:
            return sorted(self.contact_records.values(), key=lambda item: item.id)
        contacts: dict[str, CalendarContact] = {}
        for event_id, event in self.events.items():
            for person in event.people:
                identity = person.contact_id
                if identity is None:
                    continue
                contact = contacts.setdefault(identity, CalendarContact(id=identity))
                contact.evidence.append(
                    ContactEvidence(
                        event_id=event_id,
                        address=person.address,
                        name=person.name,
                        role=person.role,
                        participation=person.participation,
                    )
                )
        return sorted(contacts.values(), key=lambda item: item.id)


class CalendarArchive:
    """One instance owned by the single store writer; no vault writes."""

    def __init__(self, state_dir: Path) -> None:
        self.root = state_dir / "calendars"
        self._lock = threading.RLock()

    def catalog(self) -> CalendarCatalog:
        path = self.root / "catalog.json"
        if not path.exists():
            return CalendarCatalog()
        return CalendarCatalog.model_validate_json(path.read_bytes())

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

    def submit(self, calendar_id: str, filename: str, data: bytes) -> ImportReceipt:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", calendar_id):
            raise CalendarImportError("calendar identity must be a short lowercase slug")
        if len(data) > MAX_UPLOAD:
            raise CalendarImportError("calendar upload exceeds 10 MiB")
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
                events = parse_bounded(receipt.calendar_id, receipt.filename, original)
                merged = merge_events(catalog.events, events)
            except CalendarImportError as exc:
                receipt.state = "failed"
                receipt.error = str(exc)
                self._save_receipt(receipt)
                return receipt
            contacts = {item.id: item.model_copy(deep=True) for item in catalog.contacts()}
            for event in events:
                for person in event.people:
                    identity = person.contact_id
                    if identity is None:
                        continue
                    contact = contacts.setdefault(identity, CalendarContact(id=identity))
                    evidence = ContactEvidence(
                        event_id=event.identity,
                        address=person.address,
                        name=person.name,
                        role=person.role,
                        participation=person.participation,
                        source_import=receipt.id,
                    )
                    if evidence not in contact.evidence:
                        contact.evidence.append(evidence)
            updated = CalendarCatalog(
                events=merged, imports=[*catalog.imports, receipt.id], contact_records=contacts
            )
            receipt.event_count = len(events)
            # Persist the count before promotion so crash recovery can finish the receipt.
            self._save_receipt(receipt)
            atomic_write_text(self.root / "catalog.json", updated.model_dump_json(), mode=0o600)
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
