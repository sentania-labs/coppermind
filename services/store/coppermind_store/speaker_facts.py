"""What a recording's voices did, worked out from the transcript.

Nothing here asks a model. Talk time and the lines a voice said are read from the transcript
itself, so every quote is verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# An identity the service gave a voice that nobody has named. A name is never written here.
UNKNOWN = re.compile(r"Unknown voice \d+(?: \(recording-local\))?")


@dataclass(frozen=True)
class Heard:
    """One voice heard in a recording: who it is when that is known."""

    label: str
    seconds: float
    name: str = ""
    owner: bool = False


QUOTES_PER_VOICE = 3
# Far enough apart that three quotes are three moments, not one stretch of talk.
QUOTE_SPREAD_SECONDS = 20

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
# Characters that would become markup in a note, or a quote that is not one line of speech.
_UNSAFE = re.compile(r"[\[\]<>|`#*\\\"\u2013\u2014]|https?://|\bwww\.")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'.-]*")


def clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


def _span(segment: dict[str, Any]) -> tuple[float, float] | None:
    start, end = segment.get("start"), segment.get("end")
    if isinstance(start, bool) or isinstance(end, bool):
        return None
    if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end >= start:
        return float(start), float(end)
    return None


def talk_seconds(transcript: dict[str, Any]) -> dict[str, float]:
    spent: dict[str, float] = {}
    for segment in transcript.get("segments", []):
        label, span = segment.get("speaker"), _span(segment)
        if isinstance(label, str) and span:
            spent[label] = spent.get(label, 0.0) + span[1] - span[0]
        elif isinstance(label, str):
            spent.setdefault(label, 0.0)
    return spent


def _score(sentence: str) -> int:
    words = _WORD.findall(sentence)
    long = {w.lower() for w in words if len(w) >= 6}
    named = sum(1 for w in words[1:] if w[0].isupper() or any(c.isdigit() for c in w))
    return len(long) + named


def quotes(
    transcript: dict[str, Any], label: str, count: int = QUOTES_PER_VOICE
) -> list[tuple[str, str]]:
    """Up to `count` short lines this voice said, word for word, each with its time.
    A line with detail in it (products, numbers, long words) identifies a voice better than
    "yes, right", so those are preferred, and the chosen lines are spread over the recording."""
    # (score, start, place in the transcript, the line)
    candidates: list[tuple[int, float, int, str]] = []
    longest: tuple[int, float, str] | None = None
    for place, segment in enumerate(transcript.get("segments", [])):
        if segment.get("speaker") != label or not isinstance(segment.get("text"), str):
            continue
        span = _span(segment)
        start = span[0] if span else 0.0
        text = " ".join(segment["text"].split())
        for part, sentence in enumerate(_SENTENCE.split(text)):
            words = len(sentence.split())
            if 5 <= words <= 28 and 25 <= len(sentence) <= 130 and not _UNSAFE.search(sentence):
                candidates.append((_score(sentence), start, place * 1000 + part, sentence))
        if longest is None or len(text) > longest[0]:
            longest = (len(text), start, text)
    if not candidates and longest and len(longest[2].split()) >= 4:
        clipped = longest[2][:140].rsplit(" ", 1)[0] if len(longest[2]) > 140 else longest[2]
        if not _UNSAFE.search(clipped):
            more = "..." if clipped != longest[2] else ""
            candidates.append((0, longest[1], 0, clipped + more))
    chosen: list[tuple[int, float, int, str]] = []
    # Far apart first, then at least a different turn of speech, then anything.
    for gap in (QUOTE_SPREAD_SECONDS, 1, 0):
        for item in sorted(candidates, key=lambda c: (-c[0], c[1], c[2])):
            if len(chosen) >= count:
                break
            if item in chosen or any(abs(item[1] - c[1]) < gap for c in chosen):
                continue
            chosen.append(item)
    return [
        (text, clock(start)) for _, start, _, text in sorted(chosen, key=lambda c: (c[1], c[2]))
    ]


def owner_person_id(context: Any) -> str:
    owner = context.read("owner", {})
    return owner.get("person_id", "") if isinstance(owner, dict) else ""


def heard(
    context: Any,
    job_id: str,
    transcript: dict[str, Any],
) -> tuple[list[Heard], str]:
    """Every voice in the recording, and the label the owner was heard under ("" when the owner
    is not known or not heard)."""
    people = {
        e.id: e
        for e in context.store.entities.load().entities.values()
        if e.kind == "person" and not e.retired_at and not e.redirect
    }
    by_name: dict[str, list[Any]] = {}
    for entity in people.values():
        by_name.setdefault(entity.name.casefold(), []).append(entity)
    basis = context.read("voice-basis/" + job_id, {})
    owner = owner_person_id(context)
    found: list[Heard] = []
    owner_label = ""
    for label, seconds in talk_seconds(transcript).items():
        if UNKNOWN.fullmatch(label):
            found.append(Heard(label=label, seconds=seconds))
            continue
        entity = people.get(basis.get(label) or "")
        if entity is None and len(by_name.get(label.casefold(), [])) == 1:
            entity = by_name[label.casefold()][0]
        is_owner = bool(entity and owner and entity.id == owner)
        if is_owner:
            owner_label = label
        found.append(Heard(label=label, seconds=seconds, name=label, owner=is_owner))
    return found, owner_label
