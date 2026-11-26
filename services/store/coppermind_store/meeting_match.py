"""Conservative time and transcript evidence for recording meeting candidates."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

EARLY_START = timedelta(minutes=10)


def overlapping(events: list[dict[str, Any]], captured_at: str) -> list[dict[str, Any]]:
    """Timed occurrences holding the recording start (a few minutes early), nearest first."""
    try:
        captured = datetime.fromisoformat(captured_at)
        if captured.utcoffset() is None:
            return []
    except (ValueError, TypeError):
        return []
    found: list[tuple[timedelta, dict[str, Any]]] = []
    for event in events:
        try:
            start = datetime.fromisoformat(event["start"])
            end = datetime.fromisoformat(event["end"])
            if (
                start.utcoffset() is not None
                and end.utcoffset() is not None
                and start - EARLY_START <= captured < end
            ):
                found.append((abs(start - captured), event))
        except (KeyError, TypeError, ValueError):
            continue
    return [event for _, event in sorted(found, key=lambda item: item[0])]


def evidence(event: dict[str, Any], transcript: str) -> tuple[int, int, bool]:
    """How much of the transcript backs one event: the full names of its people the
    recording says, and whether it says the event's title."""
    text = transcript.casefold()
    names = {str(person.get("name", "")).strip().casefold() for person in event.get("people", [])}
    names = {name for name in names if len(name.split()) >= 2 and len(name) >= 5}
    named = sum(bool(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text)) for name in names)
    title = str(event.get("title", "")).strip().casefold()
    topic = len(title) >= 8 and bool(re.search(r"(?<!\w)" + re.escape(title) + r"(?!\w)", text))
    return named + int(topic), named, topic


def supported(event: dict[str, Any], transcript: str) -> bool:
    """The one meeting holding the recording start counts only when the recording also says
    one of its attendees or its title; being the only meeting on at that time is not enough."""
    return evidence(event, transcript)[0] >= 1


def suggest(events: list[dict[str, Any]], transcript: str) -> str | None:
    """Pick only uniquely supported full-name and topic evidence, otherwise abstain."""
    if len(events) < 2:
        return None
    scores = [(*evidence(event, transcript), event.get("event_id")) for event in events]
    scores.sort(key=lambda item: item[0], reverse=True)
    top = scores[0]
    if top[0] >= 2 and top[0] > scores[1][0] and top[3]:
        return str(top[3])
    return None
