"""Bounded calendar exports, with identities independent of export filenames.

Parsing never fetches URLs or extracts archive entries onto the filesystem.
A caller must archive the input before promoting the returned records.
"""

from __future__ import annotations

import hashlib
import io
import stat
import zipfile
from datetime import UTC, date, datetime
from pathlib import PurePosixPath
from typing import Any

from icalendar import Calendar
from pydantic import BaseModel, ConfigDict, Field

MAX_UPLOAD = 10 * 1024 * 1024
MAX_EXPANDED = 40 * 1024 * 1024
MAX_ENTRIES = 100
MAX_EVENTS = 50_000


class CalendarImportError(ValueError):
    """An export cannot be promoted in its entirety."""


class CalendarPerson(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str
    name: str = ""
    role: str
    participation: str = ""

    @property
    def contact_id(self) -> str | None:
        # Preserve local-part case: lowercasing it can conflate distinct mailboxes.
        if not self.address.lower().startswith("mailto:"):
            return None
        address = self.address[7:].strip()
        local, separator, domain = address.rpartition("@")
        if not separator or not local or not domain or "?" in address:
            return None
        canonical = f"{local}@{domain.lower()}"
        return hashlib.sha256(canonical.encode()).hexdigest()


class CalendarEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calendar_id: str
    uid: str
    recurrence_id: str = ""
    sequence: int = 0
    modified: str = ""
    title: str = ""
    start: str = ""
    end: str = ""
    cancelled: bool = False
    people: list[CalendarPerson] = Field(default_factory=list)
    component: str
    timezone_components: list[str] = Field(default_factory=list)
    source_entry: str

    @property
    def identity(self) -> str:
        recurrence = self.recurrence_id
        if "T" in recurrence:
            timestamp = datetime.fromisoformat(recurrence)
            if timestamp.tzinfo is not None:
                recurrence = timestamp.astimezone(UTC).isoformat()
        return hashlib.sha256(f"{self.calendar_id}\0{self.uid}\0{recurrence}".encode()).hexdigest()

    @property
    def revision(self) -> tuple[int, str]:
        value = datetime.fromisoformat(self.modified) if self.modified else None
        stamp = value.astimezone(UTC).isoformat() if value is not None else ""
        return self.sequence, stamp

    @property
    def semantic_hash(self) -> str:
        # Export ordering/filename and DTSTAMP can change without an event edit.
        component = Calendar.from_ical(self.component)
        component.pop("DTSTAMP", None)
        return hashlib.sha256(
            component.to_ical(sorted=True) + "".join(sorted(self.timezone_components)).encode()
        ).hexdigest()


def _entries(filename: str, data: bytes, limit: int = MAX_UPLOAD) -> list[tuple[str, bytes]]:
    # ``limit`` is the ceiling for this buffer only. Extracted archive entries arrive with
    # MAX_EXPANDED, already summed by the loop below, not the 10 MiB upload ceiling that
    # the original buffer passed at the intake boundary.
    if len(data) > limit:
        raise CalendarImportError(f"calendar upload exceeds {limit // (1024 * 1024)} MiB")
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix == ".ics":
        return [(filename, data)]
    if suffix != ".zip":
        raise CalendarImportError("expected an ICS file or ZIP containing ICS files")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES:
                raise CalendarImportError("archive exceeds 100 entries")
            result = []
            total = 0
            names: set[str] = set()
            for entry in entries:
                path = PurePosixPath(entry.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in entry.filename
                    or ":" in entry.filename
                    or "\0" in entry.filename
                    or entry.filename in names
                    or stat.S_ISLNK(entry.external_attr >> 16)
                    or entry.flag_bits & 1
                ):
                    raise CalendarImportError("archive has unsafe, duplicate or encrypted entries")
                names.add(entry.filename)
                if entry.is_dir():
                    continue
                if path.suffix.lower() != ".ics":
                    raise CalendarImportError("archive must contain only ICS files and directories")
                total += entry.file_size
                if total > MAX_EXPANDED:
                    raise CalendarImportError("expanded archive exceeds 40 MiB")
                with archive.open(entry) as stream:
                    content = stream.read(MAX_EXPANDED + 1)
                if len(content) != entry.file_size:
                    raise CalendarImportError("archive entry size does not match its directory")
                result.append((entry.filename, content))
            if not result:
                raise CalendarImportError("archive contains no ICS files")
            return result
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as exc:
        raise CalendarImportError("archive could not be read") from exc


def _time(component: Any, key: str, *, required: bool = False) -> str:
    value = component.get(key)
    if value is None:
        if required:
            raise CalendarImportError(f"event is missing {key}")
        return ""
    decoded = getattr(value, "dt", None)
    if not isinstance(decoded, (datetime, date)):
        raise CalendarImportError(f"event has invalid {key}")
    if isinstance(decoded, datetime) and decoded.tzinfo is None and value.params.get("TZID"):
        raise CalendarImportError("event references an unknown timezone")
    # Floating and all-day values stay floating; the configured calendar timezone
    # is applied only when looking up occurrences, never guessed during ingestion.
    return decoded.isoformat()


def _person(value: Any, role: str) -> CalendarPerson:
    return CalendarPerson(
        address=str(value),
        name=str(value.params.get("CN", "")),
        role=role,
        participation=str(value.params.get("PARTSTAT", "")),
    )


def parse_export(
    calendar_id: str, filename: str, data: bytes, limit: int = MAX_UPLOAD
) -> list[CalendarEvent]:
    """Validate all entries before returning any event to the caller."""
    if not calendar_id.strip() or "\0" in calendar_id:
        raise CalendarImportError("calendar source identity is required")
    events: dict[str, CalendarEvent] = {}
    event_count = 0
    for entry, content in _entries(filename, data, limit):
        try:
            calendars = Calendar.from_ical(content, multiple=True)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise CalendarImportError("invalid ICS document") from exc
        if not calendars:
            raise CalendarImportError("ICS contains no calendar")
        for calendar in calendars:
            if calendar.name != "VCALENDAR" or str(calendar.get("VERSION")) != "2.0":
                raise CalendarImportError("expected a version 2.0 calendar")
            if any(getattr(item, "errors", ()) for item in calendar.walk()):
                raise CalendarImportError("ICS contains malformed properties")
            if any(item.name not in {"VEVENT", "VTIMEZONE"} for item in calendar.subcomponents):
                raise CalendarImportError("only events and timezone definitions are supported")
            timezones = [item.to_ical().decode() for item in calendar.walk("VTIMEZONE")]
            for component in calendar.walk("VEVENT"):
                event_count += 1
                if event_count > MAX_EVENTS:
                    raise CalendarImportError("export exceeds 50000 event records")
                uid = str(component.get("UID", ""))
                if not uid or "\0" in uid:
                    raise CalendarImportError("event is missing a valid UID")
                cancelled = (
                    str(component.get("STATUS", "")).upper() == "CANCELLED"
                    or str(calendar.get("METHOD", "")).upper() == "CANCEL"
                )
                if cancelled:
                    component["STATUS"] = "CANCELLED"
                modified = _time(component, "LAST-MODIFIED")
                if modified:
                    stamp = datetime.fromisoformat(modified)
                    if stamp.tzinfo is None:
                        raise CalendarImportError("LAST-MODIFIED requires a timezone")
                people = []
                organizer = component.get("ORGANIZER")
                if organizer is not None:
                    if isinstance(organizer, list):
                        raise CalendarImportError("event has multiple organizers")
                    people.append(_person(organizer, "organizer"))
                attendees = component.get("ATTENDEE", [])
                if not isinstance(attendees, list):
                    attendees = [attendees]
                people.extend(_person(value, "invitee") for value in attendees)
                try:
                    sequence = int(component.get("SEQUENCE", 0))
                except (ValueError, TypeError) as exc:
                    raise CalendarImportError("event has invalid SEQUENCE") from exc
                if sequence < 0:
                    raise CalendarImportError("event has negative SEQUENCE")
                event = CalendarEvent(
                    calendar_id=calendar_id,
                    uid=uid,
                    recurrence_id=_time(component, "RECURRENCE-ID"),
                    sequence=sequence,
                    modified=modified,
                    title=str(component.get("SUMMARY", "")),
                    start=_time(component, "DTSTART", required=not cancelled),
                    end=_time(component, "DTEND"),
                    cancelled=cancelled,
                    people=people,
                    component=component.to_ical().decode(),
                    timezone_components=timezones,
                    source_entry=entry,
                )
                previous = events.get(event.identity)
                if previous is None or event.revision > previous.revision:
                    events[event.identity] = event
                elif (
                    event.revision == previous.revision
                    and event.semantic_hash != previous.semantic_hash
                ):
                    raise CalendarImportError("conflicting event versions within export")
    return list(events.values())


def merge_events(
    current: dict[str, CalendarEvent], incoming: list[CalendarEvent]
) -> dict[str, CalendarEvent]:
    """A partial export never removes absent records; equal-version conflicts fail."""
    merged = dict(current)
    for event in incoming:
        previous = merged.get(event.identity)
        if previous is None or event.revision > previous.revision:
            merged[event.identity] = event
        elif event.revision == previous.revision and event.semantic_hash != previous.semantic_hash:
            raise CalendarImportError("event revision conflicts with the stored calendar")
    return merged
