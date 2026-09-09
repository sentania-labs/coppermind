"""Portable file and folder naming for the notes filesystem.

The notes filesystem is synced to Windows, macOS, iOS and Android devices by
Obsidian Sync, so a name that only Linux accepts would break on arrival. These
rules run before anything is written:

- Unicode is normalised to NFC, because macOS and Linux disagree about NFD.
- Characters Windows forbids in a name are replaced with a space.
- Windows device names (CON, PRN, AUX, NUL, COM1..COM9, LPT1..LPT9) are
  suffixed so they cannot collide with a device.
- Trailing dots and spaces are stripped, which Windows silently does itself.
- The stem is capped at 120 characters so a deep folder path stays inside the
  260 character limit older Windows tooling still enforces.
- A name that already exists, compared case insensitively because Windows and
  macOS compare that way, gains a " (2)", " (3)" and so on.

The same stem function derives folder names, so an account folder and a note
filename are sanitised identically.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from datetime import date

# Windows forbids these in a path component; the control range is forbidden too.
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{n}" for n in range(1, 10)),
    *(f"LPT{n}" for n in range(1, 10)),
}

MAX_STEM_LENGTH = 120

# A filename component is limited in BYTES, not characters: 255 on ext4, APFS
# and every filesystem this is likely to land on. A title in a script whose
# characters cost three bytes each would blow that at about 85 characters while
# still satisfying the 120 character cap, and the note would fail to create with
# ENAMETOOLONG. The `.md` suffix is reserved out of the budget here, and callers
# that append anything else (a date prefix, a collision suffix) take their own
# reservation out of both budgets before asking for a fit.
MAX_COMPONENT_BYTES = 255
MAX_STEM_BYTES = MAX_COMPONENT_BYTES - len(b".md")


def _fit(stem: str, *, max_chars: int, max_bytes: int) -> str:
    """Trim `stem` to satisfy both budgets, never splitting a character.

    Slicing is done by code point rather than by byte, so a multi-byte
    character is dropped whole rather than cut in half into invalid UTF-8.
    """
    if max_chars <= 0 or max_bytes <= 0:
        return ""
    if len(stem) > max_chars:
        stem = stem[:max_chars]
    while stem and len(stem.encode("utf-8")) > max_bytes:
        stem = stem[:-1]
    return stem.rstrip(". ").strip()


def sanitize_stem(value: str, *, fallback: str = "Untitled") -> str:
    """Return `value` as a portable file or folder name, without an extension."""
    stem = unicodedata.normalize("NFC", value)
    stem = _FORBIDDEN.sub(" ", stem)
    stem = _WHITESPACE.sub(" ", stem).strip()
    stem = stem.strip(". ").strip()
    if not stem:
        return fallback
    if stem.upper() in _RESERVED or stem.upper().split(".")[0] in _RESERVED:
        stem = f"{stem} (name)"
    stem = _fit(stem, max_chars=MAX_STEM_LENGTH, max_bytes=MAX_STEM_BYTES)
    return stem or fallback


def note_stem(title: str, *, note_date: date | None = None, dated: bool = False) -> str:
    """Return the filename stem for a note.

    A dated type is prefixed with its ISO date so a folder listing sorts by
    day, which is how the folders read on a phone. The date prefix is applied
    after the title is capped, so the date is never truncated away.
    """
    stem = sanitize_stem(title)
    if dated and note_date is not None:
        prefix = f"{note_date.isoformat()} "
        stem = (
            _fit(
                stem,
                max_chars=MAX_STEM_LENGTH - len(prefix),
                max_bytes=MAX_STEM_BYTES - len(prefix.encode("utf-8")),
            )
            or "Untitled"
        )
        return f"{prefix}{stem}"
    return stem


def unique_stem(stem: str, taken: Iterable[str]) -> str:
    """Return `stem`, or the first free " (n)" variant of it.

    `taken` holds stems already present in the target folder. Comparison is
    case insensitive because a Windows or macOS device would treat
    "Review Notes.md" and "review notes.md" as the same file.
    """
    lowered = {value.casefold() for value in taken}
    if stem.casefold() not in lowered:
        return stem
    suffix = 2
    while True:
        tail = f" ({suffix})"
        # The suffix has to fit inside the limits too, so its cost comes out of
        # both budgets before the stem is trimmed to make room for it.
        head = _fit(
            stem,
            max_chars=MAX_STEM_LENGTH - len(tail),
            max_bytes=MAX_STEM_BYTES - len(tail.encode("utf-8")),
        )
        candidate = f"{head}{tail}"
        if candidate.casefold() not in lowered:
            return candidate
        suffix += 1


def sanitize_folder(value: str) -> str:
    """Return a portable relative folder path, one sanitised segment per level."""
    segments = [sanitize_stem(part, fallback="") for part in value.replace("\\", "/").split("/")]
    return "/".join(part for part in segments if part and part not in {".", ".."})
