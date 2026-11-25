"""Storage as Admin shows it: what is kept, and files beside notes.

Space used is added up from what is really on disk, so it changes when something
is deleted. A deleted note, file or recording leaves nothing behind to list or clean up.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from coppermind.db.models import Artifact
from coppermind.store_protocol import StoreError
from coppermind_store import artifacts

if TYPE_CHECKING:
    from coppermind_store.admin_notes import AdminNotes
    from coppermind_store.notes import LocalStore

_USAGE: dict[str, tuple[float, int]] = {}


def _tree_size(root: Path, *, skip_hidden: bool = False) -> int:
    total = 0
    for folder, names, files in os.walk(root):
        if skip_hidden:
            names[:] = [name for name in names if not name.startswith(".")]
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                continue
    return total


def _cached_size(root: Path, **options: Any) -> int:
    """A folder's size, measured at most once a minute."""
    key = str(root)
    known = _USAGE.get(key)
    if known and time.monotonic() - known[0] < 60:
        return known[1]
    measured = _tree_size(root, **options) if root.exists() else 0
    _USAGE[key] = (time.monotonic(), measured)
    return measured


class AdminStorage:
    def __init__(self, store: LocalStore, notes: AdminNotes) -> None:
        self.store = store
        self.notes = notes

    # -- space ---------------------------------------------------------------------------

    async def usage(self) -> dict[str, Any]:
        async with self.store.session_factory() as session:
            rows = (await session.execute(sa.select(Artifact.mime_type, Artifact.size_bytes))).all()
        recordings = sum(size for mime, size in rows if (mime or "").startswith("audio/"))
        files = sum(size for mime, size in rows if not (mime or "").startswith("audio/"))
        notes = await asyncio.to_thread(_cached_size, self.store.notes_root, skip_hidden=True)
        parts = [
            {"name": "Recordings", "bytes": recordings},
            {"name": "Files beside notes", "bytes": files},
            {"name": "Notes", "bytes": max(0, notes - files)},
        ]
        return {"parts": parts, "total": sum(part["bytes"] for part in parts)}

    # -- files beside notes --------------------------------------------------------------

    async def files(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("q") or "").strip()
        shown = max(6, min(int(payload.get("shown") or 6), 500))
        # A recording is managed under Recordings and imports. Audio with no recording behind
        # it (added while transcription was off) is a stored file like any other, so it is
        # listed here and can be found and removed here.
        recorded = {job.artifact_id for job in self.store.context.recordings.jobs()}
        conditions = [
            sa.or_(
                Artifact.mime_type.is_(None),
                ~Artifact.mime_type.like("audio/%"),
                Artifact.id.notin_(recorded),
            )
        ]
        async with self.store.session_factory() as session:
            total = await session.scalar(sa.select(sa.func.count()).where(*conditions))
            if query:
                pattern = (
                    "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                )
                conditions.append(
                    sa.or_(
                        Artifact.filename.ilike(pattern, escape="\\"),
                        Artifact.title.ilike(pattern, escape="\\"),
                    )
                )
            rows = list(
                (
                    await session.scalars(
                        sa.select(Artifact)
                        .where(*conditions)
                        .order_by(Artifact.received_at.desc(), Artifact.id.desc())
                        .limit(200 if query else shown)
                    )
                ).all()
            )
            citing = await artifacts.citing_notes(session, [row.id for row in rows])
        return {
            "total": int(total or 0),
            "items": [
                {
                    "id": row.id,
                    "name": row.filename or row.title,
                    "link": bool(row.url),
                    "when": row.received_at.isoformat(),
                    "bytes": row.size_bytes,
                    "used_by": [note.title for note in citing.get(row.id, [])],
                }
                for row in rows
            ],
        }

    async def file_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = await self.store.get_artifact(str(payload["id"]))
        try:
            await self.store.remove_artifact(record.id)
        except StoreError:
            raise
        _USAGE.clear()
        return {"name": record.filename or record.title}
