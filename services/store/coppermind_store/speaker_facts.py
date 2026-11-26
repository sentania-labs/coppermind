"""What a recording's Speakers section says about each voice, worked out from the transcript.

Nothing here asks a model. Talk time, the lines that identify a voice and the names people
call each other are read from the transcript itself, so every quote is verbatim and every
suggestion can say where it came from.
"""

from __future__ import annotations

import re
from typing import Any

from coppermind.speakers_section import UNKNOWN, Heard

QUOTES_PER_VOICE = 3
# Far enough apart that three quotes are three moments, not one stretch of talk.
QUOTE_SPREAD_SECONDS = 20
# A voice in this many recordings, and most of them, is probably the owner's.
OWNER_OFFER_MIN = 3
OWNER_OFFER_SHARE = 0.6

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
# Characters that would become markup in a note, or a quote that is not one line of speech.
_UNSAFE = re.compile(r"[\[\]<>|`#*\\\"\u2013\u2014]|https?://|\bwww\.")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'.-]*")

_GREETING = re.compile(
    r"\b(?:[Hh]i|[Hh]ello|[Hh]ey|[Tt]hanks|[Tt]hank you),?\s+(?P<name>[A-Z][a-z]{1,20})\b"
)
_CALLED = re.compile(r",\s+(?P<name>[A-Z][a-z]{1,20})\s*[?!.]")
_NOT_NAMES = frozenset(
    [
        "Everyone",
        "Everybody",
        "All",
        "There",
        "Folks",
        "Guys",
        "Team",
        "Sir",
        "Man",
        "So",
        "The",
        "That",
        "This",
        "It",
        "I",
        "We",
        "You",
        "They",
        "And",
        "But",
        "Yes",
        "No",
        "Okay",
        "Ok",
        "Sure",
        "Well",
        "Right",
        "Good",
        "Great",
        "Thanks",
        "Thank",
        "Today",
        "Anyway",
        "Now",
        "Then",
        "Here",
        "What",
        "How",
        "Why",
        "When",
        "Who",
        "Where",
        "Please",
        "Sorry",
        "Hello",
        "Hey",
        "Hi",
    ]
)


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


def addressed(
    transcript: dict[str, Any], known_first_names: dict[str, set[str]]
) -> dict[str, tuple[str, str, str]]:
    """For a voice that someone answered to by name ("thanks, Tom"), the name and when it was
    first used: {label: (full name or the name said, name said, clock)}. The voice that
    speaks right after the name is the one addressed. A name that is not a known first name
    must be used at least twice for the same voice to count."""
    segments = [s for s in transcript.get("segments", []) if isinstance(s.get("speaker"), str)]
    votes: dict[str, dict[str, list[float]]] = {}
    for index, segment in enumerate(segments):
        text = str(segment.get("text", ""))
        said = {
            m.group("name")
            for pattern in (_GREETING, _CALLED)
            for m in pattern.finditer(text)
            if m.group("name") not in _NOT_NAMES
        }
        if not said:
            continue
        reply = next(
            (s for s in segments[index + 1 : index + 4] if s["speaker"] != segment["speaker"]),
            None,
        )
        if reply is None:
            continue
        span = _span(segment)
        for name in said:
            votes.setdefault(reply["speaker"], {}).setdefault(name, []).append(
                span[0] if span else 0.0
            )
    found: dict[str, tuple[str, str, str]] = {}
    for label, names in votes.items():
        ranked = sorted(names.items(), key=lambda item: (-len(item[1]), item[0]))
        name, times = ranked[0]
        if len(ranked) > 1 and len(ranked[1][1]) == len(times):
            continue
        full = known_first_names.get(name.casefold(), set())
        if not full and len(times) < 2:
            continue
        shown = next(iter(full)) if len(full) == 1 else name
        found[label] = (shown, name, clock(min(times)))
    # One name for two voices is no evidence for either.
    counts: dict[str, int] = {}
    for shown, _, _ in found.values():
        counts[shown] = counts.get(shown, 0) + 1
    return {label: hit for label, hit in found.items() if counts[hit[0]] == 1}


def _first_names(context: Any, calendar: dict[str, Any]) -> dict[str, set[str]]:
    """First names of the people who could be in the recording: the matched meeting's
    invitees and the people Coppermind knows. Used only to recognise a name, never to name
    a voice."""
    names: dict[str, set[str]] = {}

    def add(full: str) -> None:
        parts = full.split()
        if len(parts) >= 2 and len(parts[0]) >= 2:
            names.setdefault(parts[0].casefold(), set()).add(full)

    for person in (calendar.get("event") or {}).get("people", []):
        if isinstance(person, dict) and isinstance(person.get("name"), str):
            add(" ".join(person["name"].split()))
    for entity in context.store.entities.load().entities.values():
        if entity.kind == "person" and not entity.retired_at and not entity.redirect:
            add(" ".join(entity.name.split()))
    return names


def owner_person_id(context: Any) -> str:
    owner = context.read("owner", {})
    return owner.get("person_id", "") if isinstance(owner, dict) else ""


def heard(
    context: Any,
    job_id: str,
    transcript: dict[str, Any],
    calendar: dict[str, Any],
) -> tuple[list[Heard], str]:
    """Every voice in the recording as the Speakers section shows it, and the label the owner
    was heard under ("" when the owner is not known or not heard)."""
    from coppermind_store.voice_store import VoiceStore

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
    spent = talk_seconds(transcript)
    voice = VoiceStore(context)
    unknowns = voice.unknown_summaries()
    appearances = {
        u["name"]: len({a["note_id"] for a in u["assignments"]}) for u in unknowns.values()
    }
    total = voice.recording_count()
    guesses = addressed(transcript, _first_names(context, calendar))
    found: list[Heard] = []
    owner_label = ""
    for label, seconds in spent.items():
        if UNKNOWN.fullmatch(label):
            count = appearances.get(label, 0)
            seen = (
                (count, total)
                if count >= OWNER_OFFER_MIN and total and count / total >= OWNER_OFFER_SHARE
                else None
            )
            guess = guesses.get(label)
            suggestion = ""
            if guess:
                shown, said, when = guess
                suggestion = (
                    f"{shown}, addressed as {said} at {when}"
                    if shown != said
                    else f"{said}, addressed by name at {when}"
                )
            found.append(
                Heard(
                    label=label,
                    seconds=seconds,
                    quotes=tuple(quotes(transcript, label)),
                    suggestion=suggestion,
                    seen=seen,
                )
            )
            continue
        person_id = basis.get(label) or ""
        entity = people.get(person_id)
        if entity is None and len(by_name.get(label.casefold(), [])) == 1:
            entity = by_name[label.casefold()][0]
        is_owner = bool(entity and owner and entity.id == owner)
        if is_owner:
            owner_label = label
        found.append(
            Heard(
                label=label,
                seconds=seconds,
                name=label,
                link=(entity.path.removesuffix(".md") if entity and entity.path else ""),
                owner=is_owner,
                how="Identified by voice (strong match).",
            )
        )
    return found, owner_label
