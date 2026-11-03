"""Explicit, receipt-bound removal. Git is never written by this module.

A receipt records the exact authorization before any unlink. Interrupted work
is resumed only by confirming that receipt again. No content is copied into it.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from coppermind import frontmatter as fm
from coppermind.atomicio import atomic_write_bytes, sync_directory
from coppermind.db.models import Note, NoteSource, Source
from coppermind.db.session import transaction
from coppermind.ids import is_valid_id
from coppermind.purge import ConfirmPurge, PurgeCandidate, PurgeCounts, PurgeResult
from coppermind.store_protocol import (
    MetadataUnavailable,
    NotesFilesystemUnavailable,
    PurgeConflict,
    ValidationFailed,
)
from coppermind_store import indexer, note_history
from coppermind_store.fs import resolve as filesystem_path
from coppermind_store.sources import _external_id_claim_path, _manifest_document

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore


PurgeChanged = PurgeConflict


def directory(store: LocalStore) -> Path:
    return store.control.store.state_dir / "purges"


def records(store: LocalStore) -> list[dict[str, Any]]:
    return [json.loads(p.read_bytes()) for p in sorted(directory(store).glob("*.json"))]


def pending_ids(store: LocalStore, kind: str) -> set[str]:
    return {
        item["candidate"]["id"]
        for record in records(store)
        if record["state"] in {"applying", "failed"}
        for item in record["items"]
        if item["candidate"]["kind"] == kind
    }


def save(store: LocalStore, record: dict[str, Any]) -> None:
    atomic_write_bytes(
        directory(store) / (record["token"] + ".json"),
        json.dumps(record, sort_keys=True).encode(),
        mode=0o600,
    )


def result(record: dict[str, Any]) -> PurgeResult:
    candidates = [PurgeCandidate.model_validate(i["candidate"]) for i in record["items"]]
    return PurgeResult(
        token=record["token"],
        state=record["state"],
        created_at=record["created_at"],
        retention_days=record["retention_days"],
        candidates=candidates,
        counts=PurgeCounts(
            notes=sum(c.kind == "note" for c in candidates),
            sources=sum(c.kind == "source" for c in candidates),
            by_origin=dict(Counter(v for c in candidates for v in c.origins)),
            by_provider=dict(Counter(v for c in candidates for v in c.providers)),
        ),
        completed=len(record.get("completed", [])),
        error=record.get("error"),
    )


def read(store: LocalStore, token: str) -> dict[str, Any]:
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise ValidationFailed(["Invalid purge receipt. Run a new preview."])
    try:
        return json.loads((directory(store) / (token + ".json")).read_bytes())
    except FileNotFoundError as exc:
        raise ValidationFailed(["Purge receipt not found. Run a new preview."]) from exc


def resolve(root: Path, relative: str) -> Path:
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in {".", "..", ".git"} for p in parts):
        raise ValueError("unsafe purge path")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("purge refuses symlinks")
    return filesystem_path(root, relative)


def _digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("purge files must be regular files")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file(store: LocalStore, area: str, path: Path) -> dict[str, str]:
    root = _root(store, area)
    relative = path.relative_to(root).as_posix()
    # resolve rejects symlinks and traversal, including ancestor symlinks.
    resolve(root, relative)
    return {"area": area, "path": relative, "hash": _digest(path)}


def _root(store: LocalStore, area: str) -> Path:
    return {
        "notes": store.notes_root,
        "sources": store.sources_root,
        "state": store.control.store.state_dir,
    }[area]


def disk_references(store: LocalStore) -> tuple[set[str], set[str]]:
    """Fail closed on unreadable notes; include unmirrored, excluded and trash notes."""
    ids: set[str] = set()
    cited: set[str] = set()
    schema = store.control.schema()
    if not store.notes_root.is_dir() or not store.sources_root.is_dir():
        raise NotesFilesystemUnavailable("Notes or sources volume unavailable")
    for path in sorted(store.notes_root.rglob("*.md")):
        relative = path.relative_to(store.notes_root)
        if any(part in {".git", ".obsidian"} for part in relative.parts):
            continue
        data = resolve(store.notes_root, relative.as_posix()).read_text()
        fields, _ = fm.parse(data)
        # Only verified generated pages are exempt from being a live citation.
        if (
            fields.get("managed") is True
            and is_valid_id(fields.get("source_id"))
            and not is_valid_id(fields.get(schema.role("id_key")))
        ):
            continue
        identity = fields.get(schema.role("id_key"))
        if isinstance(identity, str):
            ids.add(identity)
        sources = fields.get(schema.role("sources_key"), [])
        if not isinstance(sources, list) or any(not isinstance(s, str) for s in sources):
            raise ValidationFailed(["A note has unreadable source references. Fix Problems first."])
        cited.update(sources)
    return ids, cited


def _row_fingerprint(row) -> str:
    values = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


async def candidates(store: LocalStore, now: datetime) -> list[dict[str, Any]]:
    days = store.control.settings().retention.days
    cutoff = now - timedelta(days=days)
    disk_ids, disk_sources = disk_references(store)
    saved = note_history.receipts(store)
    async with store.session_factory() as session:
        notes = list((await session.scalars(sa.select(Note).order_by(Note.id))).all())
        sources = list((await session.scalars(sa.select(Source).order_by(Source.id))).all())
        linked = set(
            (
                await session.scalars(
                    sa.select(NoteSource.source_id).join(Note).where(Note.state != "deleted")
                )
            ).all()
        )
    by_note = {n.id: n for n in notes}
    by_source = {s.id: s for s in sources}
    # Missing and unparsed rows are protected too: absence is not proof of deletion.
    protected = (
        disk_sources | linked | {s for n in notes if n.state != "deleted" for s in n.source_ids}
    )
    purged_notes = {
        i["candidate"]["id"]
        for r in records(store)
        if r["state"] == "complete"
        for i in r["items"]
        if i["candidate"]["kind"] == "note"
    }
    items = []
    for note in notes:
        receipt = saved.get(note.id)
        if note.state != "deleted" or note.id in disk_ids or receipt is None:
            continue
        if receipt.content_hash != note.content_hash or receipt.path != note.path:
            continue
        deleted_at = max(note.updated_at, receipt.recorded_at)
        if deleted_at > cutoff:
            continue
        associated = [by_source[s] for s in note.source_ids if s in by_source]
        candidate = PurgeCandidate(
            kind="note",
            id=note.id,
            title=note.title or Path(note.path).stem,
            path=note.path,
            origins=sorted({s.origin or "Unspecified" for s in associated}) or ["Local notes"],
            providers=sorted({s.provider for s in associated}) or ["Local notes"],
            deleted_at=deleted_at,
        )
        paths = sorted(note_history.directory(store).glob(f"{note.id}-*.json"))
        observation = note_history.directory(store) / "observed" / f"{note.id}.json"
        if observation.exists():
            paths.append(observation)
        items.append(
            {
                "candidate": candidate.model_dump(mode="json"),
                "row": _row_fingerprint(note),
                "files": [_file(store, "state", p) for p in paths],
                "directories": [],
            }
        )
    for source in sources:
        if source.id in protected:
            continue
        if not is_valid_id(source.id):
            raise ValueError("invalid source identity")
        bundle = resolve(store.sources_root, source.id)
        for child in bundle.rglob("*"):
            resolve(store.sources_root, child.relative_to(store.sources_root).as_posix())
        manifest = _manifest_document(bundle / "manifest.json")
        opening = manifest.get("note_id")
        if not is_valid_id(opening) or opening in disk_ids:
            continue  # Legacy sources with no opening identity cannot be proved orphaned.
        owner = by_note.get(opening)
        if (owner is None and opening not in purged_notes) or (
            owner is not None and owner.state != "deleted"
        ):
            continue
        if manifest.get("source_id") != source.id:
            raise ValueError("source manifest identity mismatch")
        files = [_file(store, "sources", p) for p in sorted(bundle.rglob("*")) if not p.is_dir()]
        directories = [
            p.relative_to(store.sources_root).as_posix()
            for p in sorted(bundle.rglob("*"))
            if p.is_dir()
        ]
        directories.append(source.id)
        claim = _external_id_claim_path(
            store.sources_root, source.provider, source.external_source_id
        )
        if json.loads(claim.read_bytes()).get("source_id") != source.id:
            raise ValueError("source claim identity mismatch")
        files.append(_file(store, "sources", claim))
        projection = manifest.get("projection_path")
        if projection:
            path = resolve(store.notes_root, projection)
            if path.exists():
                fields, _ = fm.parse(path.read_text())
                if fields.get("managed") is not True or fields.get("source_id") != source.id:
                    raise ValueError("source projection identity mismatch")
                files.append(_file(store, "notes", path))
        candidate = PurgeCandidate(
            kind="source",
            id=source.id,
            title=source.external_source_id,
            path=projection or "",
            origins=[source.origin or "Unspecified"],
            providers=[source.provider],
        )
        items.append(
            {
                "candidate": candidate.model_dump(mode="json"),
                "row": _row_fingerprint(source),
                "opening": opening,
                "files": files,
                "directories": directories,
            }
        )
    return items


async def preview(store: LocalStore) -> PurgeResult:
    async with store.maintenance.exclusive():
        unfinished = [r for r in records(store) if r["state"] in {"applying", "failed"}]
        if unfinished:
            return result(unfinished[0])
        now = datetime.now(UTC)
        items = await candidates(store, now)
        record = {
            "token": secrets.token_hex(32),
            "state": "preview",
            "created_at": now.isoformat(),
            "schema_version": 1,
            "retention_days": store.control.settings().retention.days,
            "items": items,
            "completed": [],
        }
        save(store, record)
        for old in records(store):
            if old["state"] == "preview" and old.get("error") and not old.get("resolved"):
                old["resolved"] = True
                save(store, old)
        return result(record)


async def _verify_resume(store: LocalStore, record: dict[str, Any]) -> None:
    disk_ids, disk_sources = disk_references(store)
    async with store.session_factory() as session:
        notes = list((await session.scalars(sa.select(Note))).all())
        by_note = {n.id: n for n in notes}
        linked = set(
            (
                await session.scalars(
                    sa.select(NoteSource.source_id).join(Note).where(Note.state != "deleted")
                )
            ).all()
        )
        protected = (
            linked | disk_sources | {s for n in notes if n.state != "deleted" for s in n.source_ids}
        )
        for item in record["items"]:
            c = item["candidate"]
            if c["kind"] == "note":
                row = by_note.get(c["id"])
                if c["id"] in disk_ids or (row and row.state != "deleted"):
                    raise PurgeChanged()
            else:
                row = await session.get(Source, c["id"])
                owner = by_note.get(item["opening"])
                if (
                    c["id"] in protected
                    or item["opening"] in disk_ids
                    or (owner and owner.state != "deleted")
                ):
                    raise PurgeChanged()
            if row is not None and _row_fingerprint(row) != item["row"]:
                raise PurgeChanged()
            expected = {(f["area"], f["path"]) for f in item["files"]}
            for f in item["files"]:
                path = resolve(_root(store, f["area"]), f["path"])
                if path.exists() and _digest(path) != f["hash"]:
                    raise PurgeChanged()
            if c["kind"] == "source":
                for path in (store.sources_root / c["id"]).rglob("*"):
                    if (
                        not path.is_dir()
                        and ("sources", path.relative_to(store.sources_root).as_posix())
                        not in expected
                    ):
                        raise PurgeChanged()
            else:
                for path in note_history.directory(store).glob(f"{c['id']}-*.json"):
                    if (
                        "state",
                        path.relative_to(store.control.store.state_dir).as_posix(),
                    ) not in expected:
                        raise PurgeChanged()


def _unlink(store: LocalStore, item: dict[str, Any]) -> None:
    for f in item["files"]:
        path = resolve(_root(store, f["area"]), f["path"])
        if path.exists():
            if _digest(path) != f["hash"]:
                raise PurgeChanged()
            path.unlink()
            sync_directory(path.parent)
    for relative in sorted(item["directories"], key=lambda p: len(Path(p).parts), reverse=True):
        path = resolve(store.sources_root, relative)
        if path.exists():
            path.rmdir()  # Never recursively erase a file absent from the preview.
            sync_directory(path.parent)


async def confirm(store: LocalStore, request: ConfirmPurge) -> PurgeResult:
    async with store.maintenance.exclusive():
        record = read(store, request.token)
        if any(
            r["token"] != request.token and r["state"] in {"applying", "failed"}
            for r in records(store)
        ):
            raise ValidationFailed(["Finish the interrupted purge before confirming another list."])
        if record["state"] == "complete":
            return result(record)
        if record["state"] == "preview":
            if record["retention_days"] != store.control.settings().retention.days or record[
                "items"
            ] != await candidates(store, datetime.now(UTC)):
                raise PurgeChanged()
        else:
            await _verify_resume(store, record)
        record.update(state="applying", error=None)
        save(store, record)
        removal_started = False
        try:
            # Check the DB before any unlink, and commit removals together.
            async with transaction(store.session_factory) as session:
                await session.execute(sa.text("SELECT 1"))
                # Recheck the filesystem after the DB await. No await occurs
                # between this check and removal, including source references.
                if record["retention_days"] != store.control.settings().retention.days:
                    raise PurgeChanged()
                disk_ids, cited = disk_references(store)
                if any(
                    (
                        c["candidate"]["id"] in disk_ids
                        if c["candidate"]["kind"] == "note"
                        else c["candidate"]["id"] in cited or c["opening"] in disk_ids
                    )
                    for c in record["items"]
                ):
                    raise PurgeChanged()
                removal_started = True
                for item in record["items"]:
                    _unlink(store, item)
                now = datetime.now(UTC)
                for item in record["items"]:
                    c = item["candidate"]
                    if c["kind"] == "note":
                        await indexer.drop_note(session, c["id"], now)
                        await session.execute(sa.delete(Note).where(Note.id == c["id"]))
                    else:
                        await indexer.drop_source(session, c["id"], now)
                        await session.execute(sa.delete(Source).where(Source.id == c["id"]))
            record.update(
                state="complete", completed=[i["candidate"]["id"] for i in record["items"]]
            )
            save(store, record)
        except BaseException:
            record.update(
                state="failed" if removal_started else "preview",
                error=(
                    "Purge did not finish. Check storage and database health, "
                    "then retry this saved list. Git history is unchanged."
                ),
            )
            save(store, record)
            raise
        return result(record)


async def operation(store: LocalStore, request: ConfirmPurge | None = None) -> PurgeResult:
    try:
        return await preview(store) if request is None else await confirm(store, request)
    except SQLAlchemyError as exc:
        if request is None:
            preview_failure(store)
        raise MetadataUnavailable("purge metadata unavailable") from exc
    except (OSError, ValueError, fm.FrontmatterError) as exc:
        if request is None:
            preview_failure(store)
        raise NotesFilesystemUnavailable(
            "Purge could not safely read or remove files. Inspect Problems and retry."
        ) from exc


def preview_failure(store: LocalStore) -> None:
    """Best effort when the state volume itself is the failing dependency."""
    with suppress(OSError, ValueError):
        save(
            store,
            {
                "token": secrets.token_hex(32),
                "state": "preview",
                "created_at": datetime.now(UTC).isoformat(),
                "schema_version": 1,
                "items": [],
                "completed": [],
                "retention_days": store.control.settings().retention.days,
                "error": (
                    "Purge preview failed. Check unreadable files and storage health, "
                    "then preview again."
                ),
            },
        )
