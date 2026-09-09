"""Notes filesystem primitives.

Only the store imports this. Everything is relative to the notes filesystem
root, and nothing here accepts a path that would escape it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

NOTE_SUFFIX = ".md"


def content_hash(data: bytes) -> str:
    """The ETag of a note: sha256 over the exact file bytes."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def resolve(root: Path, relative: str) -> Path:
    """Resolve a path inside the notes filesystem, refusing to leave it."""
    candidate = (root / relative).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ValueError(f"path escapes the notes filesystem: {relative}")
    return candidate


def existing_stems(folder: Path) -> list[str]:
    """Note filename stems already present in `folder`."""
    if not folder.is_dir():
        return []
    return [entry.stem for entry in folder.iterdir() if entry.is_file()]


def is_writable(root: Path) -> tuple[bool, str]:
    """Check that the notes filesystem is present and writable.

    Used by readiness. A volume that mounted read only, or that a fresh
    Longhorn claim left owned by root, is the failure this catches, and it is
    worth reporting as not ready rather than failing on the first write.
    """
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".coppermind-write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, str(exc)
    return True, ""
