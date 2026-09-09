"""Atomic file writes.

Both writers of `/data` (notes in the store, control state files) use this, so
they have identical durability behaviour: write a temporary file in the target
directory, fsync it, rename it over the target, then fsync the directory. A
crash therefore leaves either the old file or the new one, never a half
written one, which is what lets reconciliation treat the filesystem as truth.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    """Write `data` to `path` atomically, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def atomic_write_text(path: Path, text: str, *, mode: int = 0o644) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def create_exclusive_bytes(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    """Create `path` with `data`, failing with FileExistsError if it exists.

    Used where the caller has already chosen a free name and must not clobber
    a file that appeared in the meantime, for example one Obsidian Sync just
    delivered.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
