"""Conservative time and transcript evidence for recording meeting candidates."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

EARLY_START = timedelta(minutes=10)


def overlapping(events: list[dict[str, Any]], captured_at: str) -> list[dict[str, Any]]:
    """Timed occurrences containing the recording start, a few minutes early allowed, nearest first."""
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


def suggest(events: list[dict[str, Any]], transcript: str) -> str | None:
    """Pick only uniquely supported full-name and topic evidence, otherwise abstain."""
    if len(events) < 2:
        return None
    text = transcript.casefold()
    scores = []
    for event in events:
        names = {
            str(person.get("name", "")).strip().casefold() for person in event.get("people", [])
        }
        names = {name for name in names if len(name.split()) >= 2 and len(name) >= 5}
        named = sum(
            bool(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text)) for name in names
        )
        title = str(event.get("title", "")).strip().casefold()
        topic = len(title) >= 8 and bool(re.search(r"(?<!\w)" + re.escape(title) + r"(?!\w)", text))
        scores.append((named + int(topic), named, topic, event.get("event_id")))
    scores.sort(key=lambda item: item[0], reverse=True)
    top = scores[0]
    if top[0] >= 2 and top[0] > scores[1][0] and top[3]:
        return str(top[3])
    return None
