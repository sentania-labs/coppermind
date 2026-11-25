"""The Speakers section at the top of a recording note: who was heard, and a place to name
the voices nobody has identified yet.

The application writes the section; the owner edits it. Everything here is plain text on
purpose, so it reads and edits the same in Obsidian, on a phone and in the companion:

    ## Speakers

    - [[People/Scott Bowe|Scott Bowe]] (you), 23 min. Identified by voice (strong match).
    - Unknown voice 7, 8 min. Not identified yet.
      - Said: "We run the older cluster on vSphere 7." ([[Sources/talk|00:03:12]])
      - Suggestion, not confirmed: Tom, addressed as Tom at 00:04:12.
      - [ ] This is me
      - Name this voice:

Typing a name after "Name this voice:" and marking the note reviewed names the voice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HEADING = "## Speakers"
NAME_PREFIX = "Name this voice:"
ME_TEXT = "This is me"
PROBLEM_PREFIX = "Could not name this voice yet:"
TARGET_TOKEN = "<<coppermind-source-transcript-target>>"

# An identity the service gave a voice that nobody has named. A name is never written here.
UNKNOWN = re.compile(r"Unknown voice \d+(?: \(recording-local\))?")

_HEADING = re.compile(r"^##[ \t]+(?!#)(.*?)[ \t#]*$")
_TOP = re.compile(r"^[-*][ \t]+(?P<text>.*)$")
_SUB = re.compile(r"^[ \t]+[-*][ \t]+(?P<text>.*)$")
_NAME = re.compile(r"^[ \t]+[-*][ \t]+" + re.escape(NAME_PREFIX) + r"[ \t]*(?P<name>.*?)[ \t]*$")
_ME = re.compile(r"^[ \t]+[-*][ \t]+\[(?P<mark>[ xX])\][ \t]+" + re.escape(ME_TEXT) + r"[ \t]*$")
_PROBLEM = re.compile(r"^[ \t]+[-*][ \t]+" + re.escape(PROBLEM_PREFIX))


@dataclass(frozen=True)
class Heard:
    """One voice heard in a recording, as the section shows it."""

    label: str
    seconds: float
    name: str = ""
    link: str = ""
    owner: bool = False
    how: str = ""
    quotes: tuple[tuple[str, str], ...] = ()
    suggestion: str = ""
    seen: tuple[int, int] | None = None


def duration_text(seconds: float) -> str:
    whole = max(0, round(seconds))
    if whole < 60:
        return f"{whole} s"
    if whole < 600:
        minutes, rest = divmod(whole, 60)
        return f"{minutes} min" + (f" {rest} s" if rest else "")
    minutes = round(whole / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def person_text(name: str, link: str) -> str:
    return f"[[{link}|{name}]]" if link and "|" not in name and "]" not in name else name


INSTRUCTION = (
    f'To name a voice, type the name after "{NAME_PREFIX}" and mark this note reviewed. '
    "That names the voice here, in other notes that used it, and in recordings to come."
)


def render(voices: list[Heard], *, owner_known: bool) -> str:
    """The section for these voices, or "" when nobody was heard. Order: the owner, then
    identified people, then unidentified voices, each by talk time."""
    if not voices:
        return ""
    ordered = sorted(
        voices,
        key=lambda v: (not v.owner, not v.name, -v.seconds, v.label),
    )
    unnamed = any(not v.name for v in ordered)
    lines = [HEADING, ""]
    if unnamed:
        lines += [INSTRUCTION, ""]
    for voice in ordered:
        spent = duration_text(voice.seconds)
        if voice.name:
            who = person_text(voice.name, voice.link) + (" (you)" if voice.owner else "")
            lines.append(f"- {who}, {spent}. {voice.how or 'Identified.'}")
            continue
        lines.append(f"- {voice.label}, {spent}. Not identified yet.")
        for text, clock in voice.quotes:
            lines.append(f'  - Said: "{text}" ([[{TARGET_TOKEN}|{clock}]])')
        if voice.suggestion:
            lines.append(f"  - Suggestion, not confirmed: {voice.suggestion}.")
        if voice.seen:
            count, total = voice.seen
            lines.append(
                f"  - Heard in {count} of your {total} recordings"
                + (", so this may be you." if not owner_known else ".")
            )
        if not owner_known:
            lines.append(f"  - [ ] {ME_TEXT}")
        lines.append(f"  - {NAME_PREFIX} ")
    return "\n".join(lines) + "\n"


@dataclass
class Entry:
    """A voice nobody has identified, with the owner's typed name if there is one."""

    label: str
    start: int
    end: int
    name_line: int | None = None
    typed: str = ""
    me: bool = False
    problem_line: int | None = None
    lines: list[str] = field(default_factory=list)


def _bounds(lines: list[str]) -> tuple[int, int] | None:
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if (m := _HEADING.match(line)) and m.group(1).strip().casefold() == "speakers"
        ),
        None,
    )
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if _HEADING.match(lines[i])), len(lines))
    return start, end


def entries(body: str) -> list[Entry]:
    """The voices in the section that have a place to type a name, in order: the
    unidentified ones, and any that were relabelled since the section was written. Anything
    else in the section is not touched."""
    lines = body.split("\n")
    bounds = _bounds(lines)
    if bounds is None:
        return []
    found: list[Entry] = []
    current: Entry | None = None

    def close(at: int) -> None:
        nonlocal current
        if current is None:
            return
        current.end = at
        # Trailing blank lines belong to the gap between entries, not to the entry.
        while current.end - 1 > current.start and not lines[current.end - 1].strip():
            current.end -= 1
        current.lines = lines[current.start : current.end]
        if UNKNOWN.fullmatch(current.label) or current.name_line is not None:
            found.append(current)
        current = None

    for index in range(bounds[0] + 1, bounds[1]):
        line = lines[index]
        top = _TOP.match(line)
        if top:
            close(index)
            text = top.group("text")
            label = UNKNOWN.match(text)
            current = Entry(
                label=label.group(0) if label else text.split(",", 1)[0].strip(),
                start=index,
                end=bounds[1],
            )
            continue
        if current is None:
            continue
        name = _NAME.match(line)
        if name:
            current.name_line, current.typed = index, name.group("name")
        mark = _ME.match(line)
        if mark and mark.group("mark") != " ":
            current.me = True
        if _PROBLEM.match(line):
            current.problem_line = index
    close(bounds[1])
    return found


def _find(body: str, label: str, typed: str) -> Entry | None:
    """The entry for this voice: by what was typed in it, which survives a relabel of its
    first line, else by its label."""
    every = entries(body)
    return next((e for e in every if clean_name(e.typed) == typed and typed), None) or next(
        (e for e in every if e.label == label), None
    )


def typed_names(body: str) -> dict[str, str]:
    """Label to the name the owner typed for it, only for voices with one."""
    return {e.label: clean_name(e.typed) for e in entries(body) if clean_name(e.typed)}


def clean_name(text: str) -> str:
    return " ".join(text.replace("*", "").replace("_", " ").split()).strip(" \"'")


_LINK = re.compile(r"\[\[(?P<target>[^\]|#]+)(?:#[^\]|]*)?(?:\|(?P<alias>[^\]]*))?\]\]")


def read_name(text: str) -> tuple[str, str]:
    """(name, linked page) from what the owner typed. A link the editor completed gives both
    the page it points at and the name it shows; plain text gives the name alone."""
    found = _LINK.search(text)
    if found:
        page = found.group("target").strip().removesuffix(".md")
        shown = (found.group("alias") or page.rsplit("/", 1)[-1]).strip()
        return clean_name(shown), page
    return clean_name(text), ""


def _inside(body: str) -> tuple[list[str], tuple[int, int]]:
    lines = body.split("\n")
    bounds = _bounds(lines)
    if bounds is None:
        raise ValueError("the note has no Speakers section")
    return lines, bounds


def _with_problem(entry_lines: list[str], reason: str) -> list[str]:
    text = f"  - {PROBLEM_PREFIX} {reason}"
    kept = [line for line in entry_lines if not _PROBLEM.match(line)]
    at = next((i for i, line in enumerate(kept) if _NAME.match(line)), len(kept))
    return [*kept[:at], text, *kept[at:]]


def note_problem(body: str, label: str, typed: str, reason: str) -> str:
    """Say, under this voice, why it could not be named, and keep what was typed."""
    lines, _ = _inside(body)
    entry = _find(body, label, typed)
    if entry is None:
        return body
    lines[entry.start : entry.end] = _with_problem(entry.lines, reason)
    return "\n".join(lines)


def name_voice(
    body: str,
    label: str,
    typed: str,
    name: str,
    link: str,
    *,
    owner: bool,
    how: str = "Named by you.",
) -> str:
    """Replace one unidentified voice's entry with the person it now is. The talk time stays."""
    lines, _ = _inside(body)
    entry = _find(body, label, typed)
    if entry is None:
        return body
    spent = re.match(r"^[-*][ \t]+" + re.escape(entry.label) + r",[ \t]*([^.]*?)\.", entry.lines[0])
    duration = f", {spent.group(1)}" if spent else ""
    who = person_text(name, link) + (" (you)" if owner else "")
    named = f"- {who}{duration}. {how}"
    if not owner:
        lines[entry.start : entry.end] = [named]
        return "\n".join(lines)
    # The owner is listed first, wherever the voice stood.
    del lines[entry.start : entry.end]
    bounds = _bounds(lines)
    assert bounds is not None
    first = next((i for i in range(bounds[0] + 1, bounds[1]) if _TOP.match(lines[i])), bounds[1])
    lines.insert(first, named)
    return "\n".join(lines)


def settle(body: str, name: str) -> str:
    """After a voice was named somewhere else: an entry that still says "Not identified yet"
    under this person's name, and that the owner has not typed into, becomes a plain line.
    The helper lines under it (quotes, the place to type a name) go, because the question
    they ask has been answered."""
    lines = body.split("\n")
    bounds = _bounds(lines)
    if bounds is None or not name:
        return body
    header = re.compile(
        r"^[-*][ \t]+" + re.escape(name) + r",[ \t]*(?P<spent>[^.]*?)\.[ \t]+Not identified yet\.$"
    )
    index, last = bounds[0] + 1, bounds[1]
    changed = False
    while index < last:
        found = header.match(lines[index])
        if not found:
            index += 1
            continue
        end = index + 1
        while end < last and (lines[end].startswith((" ", "\t"))):
            end += 1
        typed = any(
            (m := _NAME.match(line)) and clean_name(m.group("name"))
            for line in lines[index + 1 : end]
        )
        if typed:
            index = end
            continue
        lines[index:end] = [f"- {name}, {found.group('spent')}. Named by you in another recording."]
        last -= end - index - 1
        index += 1
        changed = True
    return tidy("\n".join(lines)) if changed else body


def tidy(body: str) -> str:
    """Once no voice in the section has a place to type a name, the line that explains how
    to name one has nothing left to say."""
    lines = body.split("\n")
    bounds = _bounds(lines)
    if bounds is None or entries(body):
        return body
    at = next((i for i in range(*bounds) if lines[i] == INSTRUCTION), None)
    if at is None:
        return body
    end = at + 1
    if end < len(lines) and not lines[end].strip():
        end += 1
    del lines[at:end]
    return "\n".join(lines)
