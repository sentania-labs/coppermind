"""Notes filesystem primitives.

Only the store imports this. Everything is relative to the notes filesystem
root, and nothing here accepts a path that would escape it.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path

from coppermind.store_protocol import NotesFilesystemUnavailable

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
    try:
        if not stat.S_ISDIR(folder.stat().st_mode):
            return []
        return [
            entry.stem
            for entry in folder.iterdir()
            if entry.suffix == NOTE_SUFFIX and is_note_file(entry)
        ]
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc


def is_note_file(path: Path) -> bool:
    """Return whether `path` is a file, with typed filesystem failures."""
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc


def is_writable(root: Path) -> tuple[bool, str]:
    """Check that the notes filesystem is present and writable.

    Used by readiness. A volume that mounted read only, or that a fresh
    Longhorn claim left owned by root, is the failure this catches, and it is
    worth reporting as not ready rather than failing on the first write.

    The probe is created exclusively under a name nothing else holds, and only
    that file is removed. A fixed name would truncate and then delete whatever
    a person already had at that path, inside the one directory this service
    exists to protect.
    """
    try:
        root.mkdir(parents=True, exist_ok=True)
        handle, probe = tempfile.mkstemp(prefix=".coppermind-write-probe.", dir=root)
        try:
            os.close(handle)
        finally:
            os.unlink(probe)
    except OSError as exc:
        return False, str(exc)
    return True, ""
