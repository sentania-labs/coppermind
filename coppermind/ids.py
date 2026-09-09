"""Note and source identifiers.

Identity is a ULID: 26 characters, Crockford base32, sortable by creation
time, safe inside a filename, a URL and YAML frontmatter without quoting.
An identifier is assigned once and never changes, so a note keeps it across
renames, moves and a full rebuild of the database from the notes filesystem.
"""

from __future__ import annotations

import re

from ulid import ULID

# Crockford base32 as ULID uses it: I, L, O and U are excluded.
_ULID_RE = re.compile(r"^[0-7][0-9ABCDEFGHJKMNPQRSTVWXYZ]{25}$")


def new_id() -> str:
    """Return a fresh identifier."""
    return str(ULID())


def is_valid_id(value: object) -> bool:
    """True when `value` is a well formed identifier."""
    return isinstance(value, str) and bool(_ULID_RE.match(value))
