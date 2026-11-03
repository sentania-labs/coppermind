"""Resource-limited calendar parser and recurrence lookup subprocess."""

from __future__ import annotations

import base64
import json
import resource
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from itertools import groupby
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import recurring_ical_events
from icalendar import Calendar

from coppermind_store.calendar_import import (
    MAX_UPLOAD,
    CalendarEvent,
    CalendarImportError,
    parse_export,
)


def isolated_calendar(payload: dict[str, Any]) -> Any:
    memory_mib = int(payload.get("memory_mib", 1024))
    if not 128 <= memory_mib <= 2048:
        raise CalendarImportError("calendar memory budget must be between 128 and 2048 MiB")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "coppermind_store.calendar_worker", str(memory_mib)],
            input=json.dumps(payload).encode(),
            capture_output=True,
            timeout=45,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CalendarImportError("calendar processing exceeded its time limit") from exc
    if result.returncode:
        raise CalendarImportError(
            "calendar processing failed or exceeded its configured memory/CPU budget: "
            f"limits.calendar_import_memory_mib={memory_mib}"
        )
    body = json.loads(result.stdout)
    if "error" in body:
        raise CalendarImportError(body["error"])
    return body["result"]


def parse_bounded(
    calendar_id: str,
    filename: str,
    data: bytes,
    limit: int = MAX_UPLOAD,
    *,
    max_events: int = 50000,
    memory_mib: int = 1024,
) -> list[CalendarEvent]:
    from coppermind_store.calendar_import import check_import_budget

    check_import_budget(filename, data, limit, max_events, memory_mib)
    result = isolated_calendar(
        {
            "action": "parse",
            "memory_mib": memory_mib,
            "max_events": max_events,
            "calendar_id": calendar_id,
            "filename": filename,
            "limit": limit,
            "data": base64.b64encode(data).decode(),
        }
    )
    timezones = result["timezones"]
    return [
        CalendarEvent.model_validate(
            item | {"timezone_components": [timezones[i] for i in item["timezone_components"]]}
        )
        for item in result["events"]
    ]


def occurrences(events: Iterable[CalendarEvent], day: date, timezone: str) -> list[dict[str, Any]]:
    """Spool ordered database rows; never send the entire calendar through JSON IPC."""
    if isinstance(events, list):
        events = sorted(events, key=lambda event: (event.calendar_id, event.uid))
    with tempfile.TemporaryDirectory(prefix="coppermind-calendar-") as directory:
        path = Path(directory) / "events.jsonl"
        with path.open("w") as handle:
            for event in events:
                handle.write(event.model_dump_json() + "\n")
        return isolated_calendar(
            {
                "action": "lookup-stream",
                "events_file": str(path),
                "day": day.isoformat(),
                "timezone": timezone,
            }
        )


def _lookup_stream(payload: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    with Path(payload["events_file"]).open() as handle:
        records = (json.loads(line) for line in handle)
        for _, group in groupby(records, key=lambda event: (event["calendar_id"], event["uid"])):
            # Keep a recurrence master and its overrides together, with a bound
            # even for adversarial exports containing thousands of overrides.
            batch = []
            for event in group:
                batch.append(event)
                if len(batch) > 1000:
                    raise CalendarImportError("one recurrence series exceeds 1000 event records")
            output.extend(_lookup(payload | {"events": batch}))
            if len(output) > 200:
                raise CalendarImportError("matching window exceeds 200 occurrences")
    return output


def _same_occurrence(left: str, right: str) -> bool:
    if "T" in left and "T" in right:
        return datetime.fromisoformat(left) == datetime.fromisoformat(right)
    return left == right


def _lookup(payload: dict[str, Any]) -> list[dict[str, Any]]:
    day = date.fromisoformat(payload["day"])
    zone = ZoneInfo(payload["timezone"])
    start = datetime.combine(day - timedelta(days=1), time.min, zone)
    end = datetime.combine(day + timedelta(days=2), time.min, zone)
    events = [CalendarEvent.model_validate(item) for item in payload["events"]]
    master_cancelled = {
        (event.calendar_id, event.uid)
        for event in events
        if event.cancelled and not event.recurrence_id
    }
    calendars: dict[str, Calendar] = {}
    definitions: set[tuple[str, str]] = set()
    for event in events:
        if (event.calendar_id, event.uid) in master_cancelled:
            continue
        calendar = calendars.setdefault(event.calendar_id, Calendar())
        component = Calendar.from_ical(event.component)
        if event.cancelled and event.recurrence_id and "DTSTART" not in component:
            component["DTSTART"] = component["RECURRENCE-ID"]
        calendar.add_component(component)
        for definition in event.timezone_components:
            key = event.calendar_id, definition
            if key not in definitions:
                calendar.add_component(Calendar.from_ical(definition))
                definitions.add(key)
    output = []
    for calendar_id, calendar in calendars.items():
        for component in recurring_ical_events.of(calendar).between(start, end):
            if str(component.get("STATUS", "")).upper() == "CANCELLED":
                continue
            uid = str(component["UID"])
            candidates = [
                event for event in events if event.calendar_id == calendar_id and event.uid == uid
            ]
            recurrence = component.get("RECURRENCE-ID")
            recurrence_id = recurrence.dt.isoformat() if recurrence is not None else ""
            event = next(
                (
                    item
                    for item in candidates
                    if _same_occurrence(item.recurrence_id, recurrence_id)
                ),
                next((item for item in candidates if not item.recurrence_id), candidates[0]),
            )
            output.append(
                {
                    "event_id": event.identity,
                    "calendar_id": calendar_id,
                    "uid": uid,
                    "occurrence": recurrence_id,
                    "title": str(component.get("SUMMARY", "")),
                    "start": component.decoded("DTSTART").isoformat(),
                    "people": [
                        person.model_dump() | {"contact_id": person.contact_id}
                        for person in event.people
                    ],
                }
            )
            if len(output) > 200:
                raise CalendarImportError("matching window exceeds 200 occurrences")
    return output


def _identify(payload: dict[str, Any]) -> list[dict[str, str]]:
    from coppermind_store.calendar_import import (
        MAX_SERIALIZED,
        _calendars,
        _entries,
        _parse_calendar,
    )

    data = base64.b64decode(payload["data"], validate=True)
    from coppermind_store.calendar_import import check_import_budget

    check_import_budget(
        payload["filename"],
        data,
        MAX_UPLOAD,
        int(payload.get("max_events", 50000)),
        int(payload.get("memory_mib", 1024)),
    )
    output = []
    # One parse per calendar serves both validation and the identity fields, so the whole
    # advertised range fits the CPU budget. Each calendar is validated on its own, as it is
    # parsed on its own after handoff, and is serialized before validation marks cancelled
    # events, so the handoff bytes are the calendar as exported.
    for filename, content in _entries(payload["filename"], data):
        for calendar in _calendars(content):
            encoded = calendar.to_ical()
            _parse_calendar("validation", filename, calendar, {}, 0)
            # The handoff bytes are the re-serialized calendar, which is a little larger
            # than the archive entry, so they are measured against MAX_SERIALIZED.
            if len(encoded) > MAX_SERIALIZED:
                raise CalendarImportError("serialized calendar exceeds 44 MiB")
            uid = calendar.get("UID", "")
            if isinstance(uid, list):
                raise CalendarImportError("calendar has multiple source identifiers")
            timezone = str(calendar.get("X-WR-TIMEZONE", ""))
            if timezone:
                try:
                    ZoneInfo(timezone)
                except (ValueError, KeyError):
                    timezone = ""
            output.append(
                {
                    "uid": str(uid),
                    "name": str(calendar.get("NAME", calendar.get("X-WR-CALNAME", ""))),
                    "timezone": timezone,
                    "entry": filename,
                    "data": base64.b64encode(encoded).decode(),
                }
            )
    if not output:
        raise CalendarImportError("export contains no calendars")
    return output


def main() -> None:
    # Enforce the budget before reading JSON/base64 input, not after allocating it.
    memory_mib = int(sys.argv[1]) if len(sys.argv) > 1 else 1024
    memory_bytes = memory_mib * 1024**2
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
    try:
        payload = json.load(sys.stdin)
        result: Any
        if payload["action"] == "parse":
            events = parse_export(
                payload["calendar_id"],
                payload["filename"],
                base64.b64decode(payload["data"], validate=True),
                int(payload.get("limit", MAX_UPLOAD)),
            )
            # Every event of a calendar carries all of its timezone definitions, so each
            # distinct definition is sent once and events refer to it by index. Otherwise
            # the output grows with events times timezones inside the 40 MiB range.
            table: dict[str, int] = {}
            records = []
            for event in events:
                record = event.model_dump()
                record["timezone_components"] = [
                    table.setdefault(text, len(table)) for text in event.timezone_components
                ]
                records.append(record)
            result = {"timezones": list(table), "events": records}
        elif payload["action"] == "lookup-stream":
            result = _lookup_stream(payload)
        elif payload["action"] == "identify":
            result = _identify(payload)
        else:
            result = _lookup(payload)
        encoded = json.dumps({"result": result})
        if len(encoded) > 96 * 1024**2:
            raise CalendarImportError("parsed export exceeds the 96 MiB record limit")
        print(encoded)
    except MemoryError:
        print(
            json.dumps(
                {
                    "error": "calendar import exceeded configured memory budget: "
                    f"limits.calendar_import_memory_mib={memory_mib}"
                }
            )
        )
    except Exception as exc:
        # Parser errors can contain calendar content. Only our fixed messages are safe.
        message = str(exc) if isinstance(exc, CalendarImportError) else "invalid calendar data"
        print(json.dumps({"error": message}))


if __name__ == "__main__":
    main()
