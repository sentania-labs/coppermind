"""Storage as Admin shows it: what is kept, the clean-up list, and files beside notes.

Space used is added up from what is really on disk, so it changes when something
is deleted. The clean-up list is the retention purge's own dry run; nothing is
removed until the owner confirms that exact list.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from coppermind.db.models import Artifact
from coppermind.entities import EntityChange
from coppermind.purge import AbandonPurge, ConfirmPurge
from coppermind.store_protocol import StoreError
from coppermind_store import artifacts, purge

if TYPE_CHECKING:
    from coppermind_store.admin_notes import AdminNotes
    from coppermind_store.notes import LocalStore

KEEP_DAYS = (7, 30, 90, 365)
KINDS = {
    "note": "Deleted note",
    "artifact": "Removed file",
    "source": "Recording of a deleted note",
    "entity": "Removed person or company",
    "copy": "Deleted file",
}
_USAGE: dict[str, tuple[float, int]] = {}
_COUNT: dict[str, tuple[float, int]] = {}
COUNT_TTL_S = 15


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

    def keep_days(self) -> int:
        return self.store.control.settings().retention.days

    # -- space ---------------------------------------------------------------------------

    def _removed_archives(self) -> int:
        total = 0
        for manifest_file in self.store.artifacts_root.glob("*/manifest.json"):
            try:
                if json.loads(manifest_file.read_bytes()).get("removed_at"):
                    total += _tree_size(manifest_file.parent)
            except (OSError, ValueError):
                continue
        return total

    async def usage(self) -> dict[str, Any]:
        async with self.store.session_factory() as session:
            rows = (await session.execute(sa.select(Artifact.mime_type, Artifact.size_bytes))).all()
        recordings = sum(size for mime, size in rows if (mime or "").startswith("audio/"))
        files = sum(size for mime, size in rows if not (mime or "").startswith("audio/"))
        state = self.store.control.store.state_dir

        def measure() -> tuple[int, int]:
            kept = (
                self._removed_archives()
                + _tree_size(state / "note-recovery")
                + _tree_size(state / "admin-removed")
            )
            return kept, _cached_size(self.store.notes_root, skip_hidden=True)

        kept, notes = await asyncio.to_thread(measure)
        parts = [
            {"name": "Recordings", "bytes": recordings},
            {"name": "Files beside notes", "bytes": files},
            {"name": "Deleted, kept for recovery", "bytes": kept},
            {"name": "Notes", "bytes": max(0, notes - files)},
        ]
        return {"parts": parts, "total": sum(part["bytes"] for part in parts)}

    # -- clean up ------------------------------------------------------------------------

    def _unfinished(self) -> dict[str, Any] | None:
        for record in purge.records(self.store):
            if record["state"] in {"applying", "failed"}:
                return record
        return None

    def _item_size(self, item: dict[str, Any]) -> int:
        total = 0
        for entry in item.get("files", []):
            try:
                root = purge._root(self.store, entry["area"])
                total += (root / entry["path"]).stat().st_size
            except (OSError, ValueError, KeyError):
                continue
        return total

    def _old_removed(self) -> list[dict[str, Any]]:
        """Files Admin itself took away that are past the keep time."""
        cutoff = datetime.now(UTC) - timedelta(days=self.keep_days())
        return [
            item
            for item in self.notes.removed()
            if datetime.fromisoformat(item["removed_at"]) <= cutoff
        ]

    def _still_kept(self) -> list[dict[str, Any]]:
        """Removed files and people not yet past the keep time: still here to bring back."""
        cutoff = datetime.now(UTC) - timedelta(days=self.keep_days())
        kept = []
        for manifest_file in self.store.artifacts_root.glob("*/manifest.json"):
            try:
                manifest = json.loads(manifest_file.read_bytes())
                removed = manifest.get("removed_at")
                if (
                    not removed
                    or manifest.get("removed_reason") not in artifacts.OWNER_REMOVAL_REASONS
                    or datetime.fromisoformat(removed) <= cutoff
                ):
                    continue
            except (OSError, ValueError):
                continue
            kept.append(
                {
                    "id": manifest_file.parent.name,
                    "kind": "artifact",
                    "what": KINDS["artifact"],
                    "title": str(manifest.get("filename") or manifest.get("title") or ""),
                    "deleted_at": removed,
                }
            )
        for entity in self.store.entities.load().entities.values():
            if entity.retired_at is None or entity.redirect or entity.retired_at <= cutoff:
                continue
            kept.append(
                {
                    "id": entity.id,
                    "kind": "entity",
                    "what": KINDS["entity"],
                    "title": entity.name,
                    "deleted_at": entity.retired_at.isoformat(),
                }
            )
        kept.sort(key=lambda item: item["deleted_at"], reverse=True)
        return kept

    async def interrupted(self) -> dict[str, Any] | None:
        record = await asyncio.to_thread(self._unfinished)
        if record is None:
            return None
        done = len(record.get("completed", []))
        return {
            "token": record["token"],
            "done": done,
            "total": len(record["items"]),
            "left": len(record["items"]) - done,
        }

    async def cleanup(self) -> dict[str, Any]:
        """The list a clean-up would delete for good, with what each item is and holds."""
        result = await purge.operation(self.store)
        record = await asyncio.to_thread(purge.read, self.store, result.token)
        done = set(record.get("completed", []))
        items = []
        for item in record["items"]:
            candidate = item["candidate"]
            if candidate["id"] in done:
                continue
            items.append(
                {
                    "id": candidate["id"],
                    "kind": candidate["kind"],
                    "what": KINDS[candidate["kind"]],
                    "title": candidate["title"],
                    "deleted_at": candidate.get("deleted_at") or "",
                    "bytes": await asyncio.to_thread(self._item_size, item),
                    "can_restore": candidate["kind"] in {"note", "artifact", "entity"},
                }
            )
        for removed in self._old_removed():
            items.append(
                {
                    "id": removed["key"],
                    "kind": "copy",
                    "what": KINDS["note" if removed["kind"] == "note" else "copy"],
                    "title": removed["title"],
                    "deleted_at": removed["removed_at"],
                    "bytes": len(base64.b64decode(removed["content"])),
                    "can_restore": True,
                }
            )
        items.sort(key=lambda item: item["deleted_at"])
        return {
            "kept": await asyncio.to_thread(self._still_kept),
            # What the list holds, as one value: the confirmation is held to the list its
            # owner was looking at, however many times the list has been drawn since.
            "token": hashlib.sha256(
                json.dumps(sorted(f"{item['kind']}:{item['id']}" for item in items)).encode()
            ).hexdigest(),
            "purge_token": result.token,
            "state": result.state,
            "items": items,
            "bytes": sum(item["bytes"] for item in items),
            "keep_days": self.keep_days(),
            "interrupted": await self.interrupted(),
            "error": result.error or "",
        }

    async def cleanup_count(self) -> dict[str, int]:
        """How many deleted things are past their keep time, counted as the list counts them."""
        cached = _COUNT.get(str(self.store.notes_root))
        if cached and time.monotonic() - cached[0] < COUNT_TTL_S:
            return {"count": cached[1]}
        try:
            found, _ = await purge.candidates(self.store, datetime.now(UTC))
        except (StoreError, SQLAlchemyError, OSError):
            return {"count": cached[1] if cached else 0}
        count = len(found) + len(self._old_removed())
        _COUNT[str(self.store.notes_root)] = (time.monotonic(), count)
        return {"count": count}

    async def cleanup_confirm(self, payload: dict[str, Any]) -> dict[str, Any]:
        before = await self.cleanup()
        if before["token"] != payload.get("token"):
            # The list changed after it was shown, so it is shown again, not acted on.
            return {"changed": True}
        await self.store.confirm_purge(
            ConfirmPurge(token=before["purge_token"], confirmation="Purge listed items")
        )
        for item in before["items"]:
            if item["kind"] == "copy":
                self.notes._drop_removed(item["id"])
        _USAGE.clear()
        _COUNT.clear()
        usage = await self.usage()
        return {
            "changed": False,
            "count": len(before["items"]),
            "bytes": before["bytes"],
            "used": usage["total"],
            "was_interrupted": before["interrupted"] is not None,
        }

    async def cleanup_cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        unfinished = await self.interrupted()
        if unfinished is None:
            return {"left": 0}
        await self.store.abandon_purge(AbandonPurge(token=unfinished["token"]))
        return {"left": unfinished["left"]}

    async def bring_back(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind, identity = str(payload["kind"]), str(payload["id"])
        _COUNT.clear()
        if kind == "note":
            result = await self.notes.restore({"id": identity})
            return {"title": result["title"], "kind": "note"}
        if kind == "copy":
            result = await self.notes.restore({"key": identity})
            return {"title": result["title"], "kind": "note"}
        if kind == "artifact":
            title = await artifacts.restore_artifact(self.store, identity)
            _USAGE.clear()
            _COUNT.clear()
            return {"title": title, "kind": "file"}
        if kind == "entity":
            ledger = self.store.entities.load()
            entity = ledger.entities.get(identity)
            if entity is None:
                raise ValueError("That person or company is no longer kept.")
            await self.store.entities.change(
                identity, EntityChange(version=entity.version, action="restore")
            )
            return {"title": entity.name, "kind": "person"}
        raise ValueError("That item cannot be brought back by itself.")

    async def set_keep(self, payload: dict[str, Any]) -> dict[str, Any]:
        days = int(payload["days"])
        if days not in KEEP_DAYS:
            raise ValueError("choose one of the listed times")
        state = self.store.control.store.read("settings")
        body = dict(state.body)
        body.setdefault("retention", {})["days"] = days
        self.store.control.store.write("settings", body, if_revision=state.revision)
        _COUNT.clear()
        return {"days": days, **await self.cleanup_count()}

    # -- files beside notes --------------------------------------------------------------

    async def files(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("q") or "").strip()
        shown = max(6, min(int(payload.get("shown") or 6), 500))
        conditions = [sa.or_(Artifact.mime_type.is_(None), ~Artifact.mime_type.like("audio/%"))]
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
        _COUNT.clear()
        return {"name": record.filename or record.title}
