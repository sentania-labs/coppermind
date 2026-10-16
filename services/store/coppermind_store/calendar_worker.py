"""Resource-limited calendar parser and recurrence lookup subprocess."""

from __future__ import annotations

import base64
import json
import resource
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import recurring_ical_events
from icalendar import Calendar

from coppermind_store.calendar_import import CalendarEvent, CalendarImportError, parse_export


def isolated_calendar(payload: dict[str, Any]) -> Any:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "coppermind_store.calendar_worker"],
            input=json.dumps(payload).encode(),
            capture_output=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CalendarImportError("calendar processing exceeded its time limit") from exc
    if result.returncode:
        raise CalendarImportError("calendar processing failed or exceeded its resource limit")
    body = json.loads(result.stdout)
    if "error" in body:
        raise CalendarImportError(body["error"])
    return body["result"]


def parse_bounded(calendar_id: str, filename: str, data: bytes) -> list[CalendarEvent]:
    result = isolated_calendar(
        {
            "action": "parse",
            "calendar_id": calendar_id,
            "filename": filename,
            "data": base64.b64encode(data).decode(),
        }
    )
    return [CalendarEvent.model_validate(item) for item in result]


def occurrences(events: list[CalendarEvent], day: date, timezone: str) -> list[dict[str, Any]]:
    """A three-day matching window, with capped results and a process deadline."""
    return isolated_calendar(
        {
            "action": "lookup",
            "events": [event.model_dump() for event in events],
            "day": day.isoformat(),
            "timezone": timezone,
        }
    )


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
    from coppermind_store.calendar_import import _entries

    data = base64.b64decode(payload["data"], validate=True)
    output = []
    for filename, content in _entries(payload["filename"], data):
        calendars = Calendar.from_ical(content, multiple=True)
        for calendar in calendars:
            encoded = calendar.to_ical()
            # Validate all content before any entry is dispatched.
            parse_export("validation", "calendar.ics", encoded)
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
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    try:
        payload = json.load(sys.stdin)
        if payload["action"] == "parse":
            events = parse_export(
                payload["calendar_id"],
                payload["filename"],
                base64.b64decode(payload["data"], validate=True),
            )
            result = [event.model_dump() for event in events]
        elif payload["action"] == "identify":
            result = _identify(payload)
        else:
            result = _lookup(payload)
        encoded = json.dumps({"result": result})
        if len(encoded) > 64 * 1024**2:
            raise CalendarImportError("parsed export exceeds the 64 MiB record limit")
        print(encoded)
    except Exception as exc:
        # Parser errors can contain calendar content. Only our fixed messages are safe.
        message = str(exc) if isinstance(exc, CalendarImportError) else "invalid calendar data"
        print(json.dumps({"error": message}))


if __name__ == "__main__":
    main()
