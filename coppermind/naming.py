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
    if len(stem) > MAX_STEM_LENGTH:
        stem = stem[:MAX_STEM_LENGTH].rstrip(". ").strip()
    return stem or fallback


def note_stem(title: str, *, note_date: date | None = None, dated: bool = False) -> str:
    """Return the filename stem for a note.

    A dated type is prefixed with its ISO date so a folder listing sorts by
    day, which is how the folders read on a phone. The date prefix is applied
    after the title is capped, so the date is never truncated away.
    """
    stem = sanitize_stem(title)
    if dated and note_date is not None:
        prefix = note_date.isoformat()
        room = MAX_STEM_LENGTH - len(prefix) - 1
        if len(stem) > room:
            stem = stem[:room].rstrip(". ").strip() or "Untitled"
        return f"{prefix} {stem}"
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
        candidate = f"{stem} ({suffix})"
        if candidate.casefold() not in lowered:
            return candidate
        suffix += 1


def sanitize_folder(value: str) -> str:
    """Return a portable relative folder path, one sanitised segment per level."""
    segments = [sanitize_stem(part, fallback="") for part in value.replace("\\", "/").split("/")]
    return "/".join(part for part in segments if part and part not in {".", ".."})
