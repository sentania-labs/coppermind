"""Bounded calendar exports, with identities independent of export filenames.

Parsing never fetches URLs or extracts archive entries onto the filesystem.
A caller must archive the input before promoting the returned records.
"""

from __future__ import annotations

import hashlib
import io
import re
import stat
import zipfile
from datetime import UTC, date, datetime
from pathlib import PurePosixPath
from typing import Any

from icalendar import Calendar
from pydantic import BaseModel, ConfigDict, Field

CALENDAR_ID = "calendar"
MAX_UPLOAD = 200_000_000
MAX_EXPANDED = 40 * 1024 * 1024
MEMORY_PER_BYTE = 40
MEMORY_PER_EVENT = 5461
# A re-serialized calendar is larger than the archive entry it came from: ``to_ical``
# re-folds long properties, adding a continuation pair per 75 columns (measured 2.3% on a
# 38 MiB single calendar). The bytes handed to the archive are that output, so they are
# measured against this ceiling instead of ``MAX_EXPANDED``, which bounds the expansion.
MAX_SERIALIZED = 44 * 1024 * 1024
MAX_ENTRIES = 100
MAX_EVENTS = 50_000
# Properties in this set still show for the event but are not part of the semantic
# hash. Providers move them without bumping SEQUENCE, and the parts the owner sees
# (title, times, location, description, attendees) are all matched from the hash's
# own fields. ``X-WR-*``/``X-PUBLISHED-TTL`` are calendar-level housekeeping and
# never appear on a VEVENT, so listing them costs nothing and keeps the set complete.
IGNORED_PROPERTIES = frozenset(
    {
        "DTSTAMP",
        "LAST-MODIFIED",
        "PARTSTAT",
        "X-WR-CALNAME",
        "X-WR-TIMEZONE",
        "X-WR-CALDESC",
        "X-PUBLISHED-TTL",
    }
)
# The plain-words name each property gets in a conflict report.
PROPERTY_LABELS = {
    "SUMMARY": "title",
    "DTSTART": "start time",
    "DTEND": "end time",
    "RECURRENCE-ID": "repeated occurrence",
    "LOCATION": "location",
    "DESCRIPTION": "description",
    "STATUS": "status",
    "ORGANIZER": "organizer",
    "ATTENDEE": "attendees",
    "RRULE": "repeating pattern",
}


class CalendarImportError(ValueError):
    """An export cannot be promoted in its entirety."""


class CalendarTransientError(CalendarImportError):
    """A time or resource limit stopped the import; the same bytes may succeed later."""


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


class EventProvenance(BaseModel):
    source_id: str
    filename: str
    entry: str
    imported_at: str
    legacy_calendar: str = ""


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
    provenance: list[EventProvenance] = Field(default_factory=list)
    source_entries: list[str] = Field(default_factory=list)
    source_entry: str
    acknowledged: bool = False

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
        return hashlib.sha256(
            _canonical(component) + "".join(sorted(self.timezone_components)).encode()
        ).hexdigest()


def _canonical(component: Any) -> bytes:
    """The component's bytes with the ignored properties removed, in property order."""
    for key in IGNORED_PROPERTIES:
        component.pop(key, None)
    for subcomponent in component.subcomponents:
        for key in IGNORED_PROPERTIES:
            subcomponent.pop(key, None)
        _strip_parameters(subcomponent)
    _strip_parameters(component)
    return component.to_ical(sorted=True)


def _strip_parameters(component: Any) -> None:
    """Drop ignored parameters, such as an attendee's PARTSTAT, from every property."""
    for name in IGNORED_PROPERTIES:
        for _key, value in component.property_items(sorted=False):
            for candidate in value if isinstance(value, list) else [value]:
                params = getattr(candidate, "params", None)
                if isinstance(params, dict):
                    params.pop(name, None)
                    params.pop("", None)


class CalendarProblem(BaseModel):
    """One event-level conflict, reported alongside a successful import."""

    model_config = ConfigDict(extra="forbid")
    event_id: str
    version: str
    reason: str


def _entries(filename: str, data: bytes, limit: int = MAX_UPLOAD) -> list[tuple[str, bytes]]:
    # ``limit`` is the ceiling for this buffer only. Extracted archive entries arrive with
    # MAX_EXPANDED, already summed by the loop below, not the external upload ceiling that
    # the original buffer passed at the intake boundary.
    if len(data) > limit:
        raise CalendarImportError(f"calendar upload exceeds {limit / 1_000_000:g} MB")
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
                    raise CalendarImportError("expanded archive exceeds 41.94 MB")
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


def _calendars(content: bytes) -> list[Calendar]:
    try:
        calendars = Calendar.from_ical(content, multiple=True)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise CalendarImportError("invalid ICS document") from exc
    if not calendars:
        raise CalendarImportError("ICS contains no calendar")
    return calendars


def _parse_calendar(
    calendar_id: str,
    entry: str,
    calendar: Calendar,
    events: dict[str, CalendarEvent],
    seen: int,
) -> int:
    """Validate one parsed calendar into ``events`` and return the records it walked."""
    if calendar.name != "VCALENDAR" or str(calendar.get("VERSION")) != "2.0":
        raise CalendarImportError("expected a version 2.0 calendar")
    if any(getattr(item, "errors", ()) for item in calendar.walk()):
        raise CalendarImportError("ICS contains malformed properties")
    if any(item.name not in {"VEVENT", "VTIMEZONE"} for item in calendar.subcomponents):
        raise CalendarImportError("only events and timezone definitions are supported")
    timezones = [item.to_ical().decode() for item in calendar.walk("VTIMEZONE")]
    count = 0
    for component in calendar.walk("VEVENT"):
        count += 1
        if seen + count > MAX_EVENTS:
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
            source_entries=[entry],
        )
        prior = events.get(event.identity)
        merged_events, clashes = merge_with_problems(
            {event.identity: prior} if prior else {}, [event]
        )
        if clashes:
            raise CalendarImportError(
                f'The export lists event "{event.title or event.uid}" '
                f"({event.start or 'date unknown'}, UID {event.uid}) twice with the same "
                "revision but different contents. Correct the event in its source "
                "calendar and export it again."
            )
        merged = merged_events[event.identity]
        merged.source_entries = sorted(set((prior.source_entries if prior else []) + [entry]))
        events[event.identity] = merged
    return count


def parse_export(
    calendar_id: str, filename: str, data: bytes, limit: int = MAX_UPLOAD
) -> list[CalendarEvent]:
    """Validate all entries before returning any event to the caller."""
    if not calendar_id.strip() or "\0" in calendar_id:
        raise CalendarImportError("calendar source identity is required")
    events: dict[str, CalendarEvent] = {}
    seen = 0
    for entry, content in _entries(filename, data, limit):
        for calendar in _calendars(content):
            seen += _parse_calendar(calendar_id, entry, calendar, events, seen)
    return list(events.values())


def merge_event_into(
    merged: dict[str, CalendarEvent],
    event: CalendarEvent,
    problems: list[CalendarProblem] | None = None,
) -> None:
    """Merge one event in place; an equal-version conflict keeps the stored copy."""
    previous = merged.get(event.identity)
    if previous is None:
        merged[event.identity] = event
        return
    if event.revision == previous.revision and event.semantic_hash != previous.semantic_hash:
        # The stored copy wins and the rest of the export still lands, so one stale
        # revision cannot cost the whole week. The conflict is reported per event.
        if problems is not None:
            problems.append(_conflict_problem(event, previous))
        return
    winner = event if event.revision > previous.revision else previous
    witnesses = list(previous.provenance)
    for witness in event.provenance:
        if witness not in witnesses:
            witnesses.append(witness)
    merged[event.identity] = winner.model_copy(
        update={
            "provenance": sorted(witnesses, key=lambda p: (p.imported_at, p.source_id, p.entry)),
        }
    )


def _conflict_problem(event: CalendarEvent, previous: CalendarEvent) -> CalendarProblem:
    return CalendarProblem(
        event_id=event.identity,
        version=event.semantic_hash,
        reason=(
            f'Event "{event.title or event.uid}" ({event.start or "date unknown"}, '
            f"UID {event.uid}, entry {event.source_entry}) conflicts with the stored "
            f"event: {_differences(event, previous)}. The stored version was kept and "
            "nothing needs doing unless this event matters to you. If it does, correct "
            "the event in its source calendar and drop the export again."
        ),
    )


def _differences(event: CalendarEvent, previous: CalendarEvent) -> str:
    """Plain-words list of what differs, without hashes or internal field names."""
    incoming = _properties(Calendar.from_ical(event.component))
    stored = _properties(Calendar.from_ical(previous.component))
    parts = []
    for key in sorted(set(incoming) | set(stored)):
        if incoming.get(key) != stored.get(key):
            parts.append(PROPERTY_LABELS.get(key, key.lower()))
    return ", ".join(parts) if parts else "the details differ"


def _properties(component: Any) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in component.items()
        if str(key) not in IGNORED_PROPERTIES
    }


def merge_events(
    current: dict[str, CalendarEvent], incoming: list[CalendarEvent]
) -> dict[str, CalendarEvent]:
    """A partial export never removes absent records; conflicts keep the stored copy."""
    merged = dict(current)
    for event in incoming:
        merge_event_into(merged, event)
    return merged


def merge_with_problems(
    current: dict[str, CalendarEvent], incoming: list[CalendarEvent]
) -> tuple[dict[str, CalendarEvent], list[CalendarProblem]]:
    """Merge every event in one pass and report each equal-revision conflict separately."""
    merged = dict(current)
    problems: list[CalendarProblem] = []
    for event in incoming:
        merge_event_into(merged, event, problems)
    return merged, problems


def check_import_budget(
    filename: str,
    data: bytes,
    limit: int,
    max_events: int,
    memory_mib: int,
) -> None:
    """Reject exports the parser subprocess could not hold under its address-space limit.

    The estimate uses the measured peak of about 30 bytes of parser memory per
    expanded byte, with a third of headroom (40 bytes per byte plus a per-event
    term), so it refuses what the RLIMIT_AS backstop would refuse.
    """
    size = 0
    count = 0
    for _, content in _entries(filename, data, limit):
        size += len(content)
        count += sum(1 for _ in re.finditer(rb"(?im)^BEGIN:VEVENT\r?$", content))
        if count > max_events:
            raise CalendarImportError(
                f"calendar event budget exceeded: limits.calendar_max_events={max_events}"
            )
    needed = size * MEMORY_PER_BYTE + count * MEMORY_PER_EVENT
    if needed > memory_mib * 1024**2:
        needed_mib = -(-needed // 1024**2)
        fits_mib = (
            max(0, (memory_mib * 1024**2 - count * MEMORY_PER_EVENT) // MEMORY_PER_BYTE) // 1024**2
        )
        raise CalendarImportError(
            f"calendar import needs about {needed_mib} MiB of parser memory; "
            f"limits.calendar_import_memory_mib={memory_mib} fits about {fits_mib} MiB of "
            f"expanded calendar data. Raise the setting to at least {needed_mib} "
            "or split the export"
        )
