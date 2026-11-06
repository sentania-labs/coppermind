"""Explicit, receipt-bound removal. Git is never written by this module.

A receipt records the exact authorization before any unlink. Interrupted work
is resumed only by confirming that receipt again. No content is copied into it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import threading
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from coppermind import frontmatter as fm
from coppermind.atomicio import atomic_write_bytes, sync_directory
from coppermind.db.models import Artifact, Note, NoteSource, Source
from coppermind.db.session import transaction
from coppermind.ids import is_valid_id
from coppermind.logging import get_logger
from coppermind.purge import (
    AbandonPurge,
    ConfirmPurge,
    PurgeBreakdown,
    PurgeCandidate,
    PurgeCounts,
    PurgeKept,
    PurgeResult,
)
from coppermind.store_protocol import (
    MetadataUnavailable,
    NotesFilesystemUnavailable,
    PurgeConflict,
    SourcesFilesystemUnavailable,
    ValidationFailed,
)
from coppermind_store import indexer, note_history
from coppermind_store.artifacts import OWNER_REMOVAL_REASONS
from coppermind_store.fs import resolve as filesystem_path
from coppermind_store.sources import _external_id_claim_path, _manifest_document

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore


PurgeChanged = PurgeConflict

log = get_logger("coppermind-store")


def directory(store: LocalStore) -> Path:
    return store.control.store.state_dir / "purges"


TERMINAL = {"complete": "done", "abandoned": "abandoned"}


def _folder(store: LocalStore, state: str) -> Path:
    return directory(store) / TERMINAL.get(state, "")


QUARANTINE = "quarantine"


def _load(path: Path) -> dict[str, Any] | None:
    """A receipt, or None when the file is unreadable or not a receipt."""
    try:
        record = json.loads(path.read_bytes())
        if (
            record["token"] != path.stem
            or not isinstance(record["state"], str)
            or not isinstance(record["items"], list)
        ):
            return None
        return dict(record)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def records(store: LocalStore, state: str | None = None) -> list[dict[str, Any]]:
    """Readable receipts in one place; unfinished and preview receipts live in the root."""
    folder = _folder(store, state or "")
    return [r for p in sorted(folder.glob("*.json")) if (r := _load(p)) is not None]


def unreadable(store: LocalStore, states: tuple[str, ...] = ("", *TERMINAL)) -> list[str]:
    """Tokens of receipt files that cannot be read, in the given locations."""
    return [
        p.stem
        for state in states
        for p in sorted(_folder(store, state).glob("*.json"))
        if _load(p) is None
    ]


def _problems_path(store: LocalStore) -> Path:
    return store.control.store.state_dir / "purge-problems.json"


_RECORD_LOCK = threading.Lock()


def _store_recorded(store: LocalStore, value: dict[str, Any]) -> None:
    atomic_write_bytes(_problems_path(store), json.dumps(value).encode(), mode=0o600)


def _change_recorded(store: LocalStore, change) -> None:
    with _RECORD_LOCK:
        value = recorded(store)
        if change(value):
            _store_recorded(store, value)


def note_damaged(
    store: LocalStore,
    source_id: str,
    provider: str = "",
    external_source_id: str = "",
    cause: str = "manifest",
) -> None:
    """Record what found a bundle damaged: `manifest` (purge or a scan) or `recovery`."""
    entry = {
        "source_id": source_id,
        "provider": provider,
        "external_source_id": external_source_id,
        "cause": cause,
    }

    def change(value: dict[str, Any]) -> bool:
        others = [
            e
            for e in value["damaged"]
            if (e["source_id"], e.get("cause", "manifest")) != (source_id, cause)
        ]
        if len(others) == len(value["damaged"]) - 1 and entry in value["damaged"]:
            return False
        value["damaged"] = [*others, entry]
        return True

    with suppress(OSError):
        _change_recorded(store, change)


def note_repaired(store: LocalStore, source_id: str, cause: str | None = None) -> None:
    """Forget what was recorded for a bundle once that condition is verified repaired."""

    def change(value: dict[str, Any]) -> bool:
        kept = [
            e
            for e in value["damaged"]
            if e["source_id"] != source_id or (cause and e.get("cause", "manifest") != cause)
        ]
        changed = len(kept) != len(value["damaged"])
        value["damaged"] = kept
        return changed

    with suppress(OSError):
        _change_recorded(store, change)


async def live_ids(store: LocalStore, ids: list[str]) -> set[str]:
    """Which of these notes are live again, and which sources a live note now cites."""
    if not ids:
        return set()
    async with store.session_factory() as session:
        notes = (
            await session.scalars(
                sa.select(Note.id).where(Note.id.in_(ids), Note.state != "deleted")
            )
        ).all()
        linked = (
            await session.scalars(
                sa.select(NoteSource.source_id)
                .join(Note, Note.id == NoteSource.note_id)
                .where(NoteSource.source_id.in_(ids), Note.state != "deleted")
            )
        ).all()
        mirrored = (
            await session.scalars(
                sa.select(sa.func.unnest(Note.source_ids)).where(
                    Note.state != "deleted", Note.source_ids.overlap(ids)
                )
            )
        ).all()
    return {*notes, *(({*linked, *mirrored}) & set(ids))}


def recorded(store: LocalStore) -> dict[str, Any]:
    """The purge problems found by the last maintenance scan. A small file, never a walk."""
    empty: dict[str, Any] = {"damaged": [], "unreadable": [], "abandoned": []}
    try:
        value = json.loads(_problems_path(store).read_bytes())
    except FileNotFoundError:
        return empty
    except ValueError:
        return empty
    return {key: value.get(key, []) for key in empty} if isinstance(value, dict) else empty


def pending_ids(store: LocalStore, kind: str) -> set[str]:
    """Ids held by unfinished purges. An unreadable receipt is skipped, not fatal."""
    return {
        item["candidate"]["id"]
        for record in records(store)
        if record["state"] in {"applying", "failed"}
        for item in record["items"]
        if item["candidate"]["kind"] == kind
    }


def save(store: LocalStore, record: dict[str, Any]) -> None:
    name = record["token"] + ".json"
    atomic_write_bytes(
        _folder(store, record["state"]) / name,
        json.dumps(record, sort_keys=True).encode(),
        mode=0o600,
    )
    here = record["state"] if record["state"] in TERMINAL else ""
    for state in ("", *TERMINAL):
        if state != here:
            (_folder(store, state) / name).unlink(missing_ok=True)


def _breakdown(candidates: list[PurgeCandidate], field: str) -> dict[str, PurgeBreakdown]:
    counts: dict[str, PurgeBreakdown] = {}
    for c in candidates:
        for value in getattr(c, field):
            entry = counts.setdefault(value, PurgeBreakdown())
            if c.kind == "note":
                entry.notes += 1
            elif c.kind == "artifact":
                entry.artifacts += 1
            else:
                entry.sources += 1
    return counts


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
            artifacts=sum(c.kind == "artifact" for c in candidates),
            by_origin=_breakdown(candidates, "origins"),
            by_provider=_breakdown(candidates, "providers"),
        ),
        kept=PurgeKept.model_validate(record.get("kept", {})),
        completed=len(record.get("completed", [])),
        remaining=record.get("remaining", []),
        error=record.get("error"),
    )


def _locate(store: LocalStore, token: str) -> Path:
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise ValidationFailed(["Invalid purge receipt. Run a new preview."])
    for state in ("", *TERMINAL):
        path = _folder(store, state) / (token + ".json")
        if path.exists():
            return path
    raise ValidationFailed(["Purge receipt not found. Run a new preview."])


def read(store: LocalStore, token: str) -> dict[str, Any]:
    record = _load(_locate(store, token))
    if record is None:
        raise ValidationFailed(["This purge receipt is unreadable. Abandon it from Problems."])
    return record


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


def _directory_root(store: LocalStore, item: dict[str, Any]) -> Path:
    return store.artifacts_root if item["candidate"]["kind"] == "artifact" else store.sources_root


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
        "artifacts": store.artifacts_root,
        "state": store.control.store.state_dir,
    }[area]


def disk_references(store: LocalStore) -> tuple[set[str], set[str], dict[str, list[str]]]:
    """Fail closed on unreadable notes; include unmirrored, excluded and trash notes."""
    ids: set[str] = set()
    cited: set[str] = set()
    pages: dict[str, list[str]] = {}
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
            pages.setdefault(fields["source_id"], []).append(relative.as_posix())
            continue
        identity = fields.get(schema.role("id_key"))
        if isinstance(identity, str):
            ids.add(identity)
        sources = fields.get(schema.role("sources_key"), [])
        if not isinstance(sources, list) or any(not isinstance(s, str) for s in sources):
            raise ValidationFailed(["A note has unreadable source references. Fix Problems first."])
        cited.update(sources)
    return ids, cited, pages


def _bundle(bundle: Path) -> tuple[str, dict[str, Any]]:
    """`empty` holds no files, `ok` has a readable manifest, `damaged` is anything else."""
    if not any(not p.is_dir() for p in bundle.rglob("*")):
        return "empty", {}
    try:
        return "ok", _manifest_document(bundle / "manifest.json")
    except (SourcesFilesystemUnavailable, OSError):
        return "damaged", {}


def damaged_bundles(store: LocalStore, source_ids: list[str]) -> list[str]:
    return [
        source_id
        for source_id in source_ids
        if is_valid_id(source_id) and _bundle(store.sources_root / source_id)[0] == "damaged"
    ]


def _row_fingerprint(row) -> str:
    values = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


async def candidates(
    store: LocalStore, now: datetime
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    days = store.control.settings().retention.days
    cutoff = now - timedelta(days=days)
    disk_ids, disk_sources, pages = await asyncio.to_thread(disk_references, store)
    saved = await asyncio.to_thread(note_history.receipts, store)
    complete = await asyncio.to_thread(records, store, "complete")
    abandoned = await asyncio.to_thread(records, store, "abandoned")
    async with store.session_factory() as session:
        notes = list((await session.scalars(sa.select(Note).order_by(Note.id))).all())
        sources = list((await session.scalars(sa.select(Source).order_by(Source.id))).all())
        live_artifacts = set((await session.scalars(sa.select(Artifact.id))).all())
        links = list(
            (
                await session.scalars(
                    sa.select(NoteSource).order_by(NoteSource.created_at, NoteSource.note_id)
                )
            ).all()
        )
        linked = set(
            (
                await session.scalars(
                    sa.select(NoteSource.source_id).join(Note).where(Note.state != "deleted")
                )
            ).all()
        )
    openings: dict[str, str] = {}
    for link in links:
        openings.setdefault(link.source_id, link.note_id)
    purged_notes = {
        i["candidate"]["id"]
        for r in complete
        for i in r["items"]
        if i["candidate"]["kind"] == "note"
    }
    # Abandon can follow a committed removal whose final receipt save failed.
    # Only the items confirmed removed by abandon are evidence of a purge.
    for record in abandoned:
        removed = set(record.get("completed", []))
        purged_notes.update(
            item["candidate"]["id"]
            for item in record["items"]
            if item["candidate"]["kind"] == "note" and item["candidate"]["id"] in removed
        )
    return await asyncio.to_thread(
        _plan,
        store,
        cutoff,
        disk_ids,
        disk_sources,
        pages,
        openings,
        saved,
        purged_notes,
        notes,
        sources,
        linked,
        abandoned,
        live_artifacts,
    )


def _removed_archives(
    store: LocalStore, cutoff: datetime, live: set[str]
) -> tuple[list[dict[str, Any]], int]:
    """Archives whose manifest marks them removed before `cutoff` and that no row holds."""
    items: list[dict[str, Any]] = []
    within = 0
    for manifest_file in sorted(store.artifacts_root.glob("*/manifest.json")):
        artifact_id = manifest_file.parent.name
        if not is_valid_id(artifact_id) or artifact_id in live:
            continue
        try:
            manifest = json.loads(manifest_file.read_bytes())
            removed_at = datetime.fromisoformat(manifest["removed_at"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if (
            not isinstance(manifest, dict)
            or manifest.get("artifact_id") != artifact_id
            or manifest.get("kind") != "artifact"
            or removed_at.utcoffset() is None
        ):
            continue
        reason = manifest.get("removed_reason")
        if reason not in OWNER_REMOVAL_REASONS:
            continue
        if removed_at > cutoff:
            within += 1
            continue
        archive = resolve(store.artifacts_root, artifact_id)
        for child in archive.rglob("*"):
            resolve(store.artifacts_root, child.relative_to(store.artifacts_root).as_posix())
        origin = str(manifest.get("origin") or "Unspecified")
        candidate = PurgeCandidate(
            kind="artifact",
            id=artifact_id,
            title=str(manifest.get("title") or manifest.get("filename") or artifact_id),
            path="",
            origins=[origin],
            providers=["Artifacts"],
            deleted_at=removed_at,
            reason=str(reason),
        )
        items.append(
            {
                "candidate": candidate.model_dump(mode="json"),
                "row": None,
                "files": [
                    _file(store, "artifacts", p)
                    for p in sorted(archive.rglob("*"))
                    if not p.is_dir()
                ],
                "directories": [
                    *(
                        p.relative_to(store.artifacts_root).as_posix()
                        for p in sorted(archive.rglob("*"))
                        if p.is_dir()
                    ),
                    artifact_id,
                ],
            }
        )
    return items, within


def _plan(
    store: LocalStore,
    cutoff: datetime,
    disk_ids: set[str],
    disk_sources: set[str],
    pages: dict[str, list[str]],
    openings: dict[str, str],
    saved: dict[str, Any],
    purged_notes: set[str],
    notes: list[Note],
    sources: list[Source],
    linked: set[str],
    abandoned: list[dict[str, Any]],
    live_artifacts: set[str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    by_note = {n.id: n for n in notes}
    by_source = {s.id: s for s in sources}
    # Missing and unparsed rows are protected too: absence is not proof of deletion.
    protected = (
        disk_sources | linked | {s for n in notes if n.state != "deleted" for s in n.source_ids}
    )
    items: list[dict[str, Any]] = []
    kept_notes = 0
    for note in notes:
        receipt = saved.get(note.id)
        if note.state != "deleted" or note.id in disk_ids:
            continue
        if receipt is not None and (
            receipt.content_hash != note.content_hash or receipt.path != note.path
        ):
            continue
        deleted_at = (
            note.updated_at if receipt is None else max(note.updated_at, receipt.recorded_at)
        )
        if deleted_at > cutoff:
            kept_notes += 1
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
            leftover=receipt is None,
        )
        paths = sorted(note_history.directory(store).glob(f"{note.id}-*.json"))
        observation = note_history.directory(store) / "observed" / f"{note.id}.json"
        if observation.exists():
            paths.append(observation)
        journal = store.control.store.state_dir / "filing" / f"{note.id}.json"
        if journal.exists():
            paths.append(journal)
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
        claim = _external_id_claim_path(
            store.sources_root, source.provider, source.external_source_id
        )
        state, manifest = _bundle(bundle)
        opening: Any
        if state == "damaged":
            note_damaged(store, source.id, source.provider, source.external_source_id)
            continue
        note_repaired(store, source.id, "manifest")
        if state == "ok":
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
        else:
            opening = openings.get(source.id)
            owner = by_note.get(opening) if opening else None
            if owner is None or owner.state != "deleted" or opening in disk_ids:
                continue
        files = [_file(store, "sources", p) for p in sorted(bundle.rglob("*")) if not p.is_dir()]
        directories = [
            p.relative_to(store.sources_root).as_posix()
            for p in sorted(bundle.rglob("*"))
            if p.is_dir()
        ]
        directories.append(source.id)
        if claim.exists():
            if json.loads(claim.read_bytes()).get("source_id") != source.id:
                raise ValueError("source claim identity mismatch")
            files.append(_file(store, "sources", claim))
        recorded = manifest.get("projection_path") if state == "ok" else None
        projections = sorted(set(pages.get(source.id, [])) | ({recorded} if recorded else set()))
        for projection in projections:
            path = resolve(store.notes_root, projection)
            if path.exists():
                fields, _ = fm.parse(path.read_text())
                if fields.get("managed") is not True or fields.get("source_id") != source.id:
                    raise ValueError("source projection identity mismatch")
                files.append(_file(store, "notes", path))
        candidate = PurgeCandidate(
            kind="source",
            id=source.id,
            title=Path(projections[0]).stem if projections else source.external_source_id,
            path=projections[0] if projections else "",
            origins=[source.origin or "Unspecified"],
            providers=[source.provider],
            leftover=state == "empty" or not claim.exists(),
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
    removed_artifacts, artifacts_within = _removed_archives(store, cutoff, live_artifacts)
    items.extend(removed_artifacts)
    seen = {i["candidate"]["id"] for i in items}
    for record in abandoned:
        held = set(record.get("remaining", []))
        for old in record["items"]:
            c = old["candidate"]
            if c["id"] not in held or c["id"] in seen:
                continue
            if c["kind"] == "note":
                if c["id"] in by_note or c["id"] in disk_ids:
                    continue
            elif c["kind"] == "artifact":
                if c["id"] in live_artifacts:
                    continue
            else:
                owner = by_note.get(old["opening"])
                if (
                    c["id"] in by_source
                    or c["id"] in protected
                    or old["opening"] in disk_ids
                    or (owner is not None and owner.state != "deleted")
                ):
                    continue
            leftover = _residual(store, old)
            if leftover is not None:
                items.append(leftover)
                seen.add(c["id"])
    kept = {
        "notes_within_retention": kept_notes,
        "sources_in_use": sum(s.id in protected for s in sources),
        "artifacts_within_retention": artifacts_within,
    }
    return items, kept


def _residual(store: LocalStore, old: dict[str, Any]) -> dict[str, Any] | None:
    """What an abandoned purge left behind, limited to the files its receipt recorded."""
    files = [f for f in old["files"] if resolve(_root(store, f["area"]), f["path"]).exists()]
    directory_root = _directory_root(store, old)
    directories = [d for d in old["directories"] if resolve(directory_root, d).exists()]
    if not files and not directories:
        return None
    item = {
        **old,
        "candidate": {**old["candidate"], "leftover": True},
        "row": None,
        "files": files,
        "directories": directories,
    }
    try:
        _verify_files(store, item)
    except PurgeChanged:
        return None
    return item


async def _scan(store: LocalStore) -> None:
    async with store.session_factory() as session:
        rows = (
            await session.execute(
                sa.select(Source.id, Source.provider, Source.external_source_id).order_by(Source.id)
            )
        ).all()
    named = {row.id: row for row in rows}
    damaged = await asyncio.to_thread(damaged_bundles, store, list(named))
    abandoned = []
    for receipt in await asyncio.to_thread(records, store, "abandoned"):
        held = set(receipt.get("remaining", []))
        live = await live_ids(store, sorted(held))
        _, remaining = await outcome(
            store,
            {
                **receipt,
                "items": [
                    i
                    for i in receipt["items"]
                    if i["candidate"]["id"] in held and i["candidate"]["id"] not in live
                ],
            },
        )
        if remaining:
            titles = {i["candidate"]["id"]: i["candidate"]["title"] for i in receipt["items"]}
            abandoned.append(
                {
                    "token": receipt["token"],
                    "removed": len(receipt.get("completed", [])),
                    "remaining_ids": remaining,
                    "titles": {r: titles[r] for r in remaining[:50]},
                }
            )
    verification = [
        e
        for e in recorded(store)["damaged"]
        if e.get("cause") == "recovery" and (store.sources_root / e["source_id"]).exists()
    ]
    value = {
        "damaged": [
            {
                "source_id": source_id,
                "provider": named[source_id].provider,
                "external_source_id": named[source_id].external_source_id,
                "cause": "manifest",
            }
            for source_id in damaged
        ]
        + verification,
        "unreadable": await asyncio.to_thread(unreadable, store, tuple(TERMINAL)),
        "abandoned": abandoned,
    }

    def write() -> None:
        with _RECORD_LOCK:
            _store_recorded(store, value)

    await asyncio.to_thread(write)


async def _rescan(store: LocalStore) -> None:
    """Refresh the recorded problems from inside a purge action that holds the gate."""
    try:
        await _scan(store)
    except Exception as exc:  # noqa: BLE001 - a stale list must not undo a finished action
        log.warning("purge problems scan deferred", reason=type(exc).__name__)


async def scan_problems(store: LocalStore) -> None:
    """The full bundle and receipt scan: store startup, or an explicit maintenance action."""
    async with store.maintenance.exclusive():
        await _rescan(store)


async def preview(store: LocalStore) -> PurgeResult:
    async with store.maintenance.exclusive():
        existing = await asyncio.to_thread(records, store)
        unfinished = [r for r in existing if r["state"] in {"applying", "failed"}]
        if unfinished:
            return result(unfinished[0])
        now = datetime.now(UTC)
        items, kept = await candidates(store, now)
        record = {
            "token": secrets.token_hex(32),
            "state": "preview",
            "created_at": now.isoformat(),
            "schema_version": 1,
            "retention_days": store.control.settings().retention.days,
            "items": items,
            "kept": kept,
            "completed": [],
        }
        save(store, record)
        for old in existing:
            if old["state"] == "preview":
                (directory(store) / (old["token"] + ".json")).unlink(missing_ok=True)
        await _rescan(store)
        return result(record)


async def _verify_resume(store: LocalStore, record: dict[str, Any]) -> None:
    disk_ids, disk_sources, _ = await asyncio.to_thread(disk_references, store)
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
            elif c["kind"] == "artifact":
                if await session.get(Artifact, c["id"]) is not None:
                    raise PurgeChanged()
                row = None
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
            await asyncio.to_thread(_verify_files, store, item)


def _verify_files(store: LocalStore, item: dict[str, Any]) -> None:
    c = item["candidate"]
    expected = {(f["area"], f["path"]) for f in item["files"]}
    for f in item["files"]:
        path = resolve(_root(store, f["area"]), f["path"])
        if path.exists() and _digest(path) != f["hash"]:
            raise PurgeChanged()
    if c["kind"] == "source":
        for path in (store.sources_root / c["id"]).rglob("*"):
            if (
                not path.is_dir()
                and ("sources", path.relative_to(store.sources_root).as_posix()) not in expected
            ):
                raise PurgeChanged()
    elif c["kind"] == "artifact":
        for path in (store.artifacts_root / c["id"]).rglob("*"):
            if (
                not path.is_dir()
                and ("artifacts", path.relative_to(store.artifacts_root).as_posix()) not in expected
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
    directory_root = _directory_root(store, item)
    for relative in sorted(item["directories"], key=lambda p: len(Path(p).parts), reverse=True):
        path = resolve(directory_root, relative)
        if path.exists():
            path.rmdir()  # Never recursively erase a file absent from the preview.
            sync_directory(path.parent)


async def confirm(store: LocalStore, request: ConfirmPurge) -> PurgeResult:
    async with store.maintenance.exclusive():
        record = read(store, request.token)
        if any(
            r["token"] != request.token and r["state"] in {"applying", "failed"}
            for r in await asyncio.to_thread(records, store)
        ):
            raise ValidationFailed(["Finish the interrupted purge before confirming another list."])
        if record["state"] == "complete":
            return result(record)
        if record["state"] == "abandoned":
            raise ValidationFailed(["This purge was abandoned. Run a new preview."])
        resuming = record["state"] != "preview"
        if not resuming:
            if (
                record["retention_days"] != store.control.settings().retention.days
                or record["items"] != (await candidates(store, datetime.now(UTC)))[0]
            ):
                raise PurgeChanged()
        else:
            await _verify_resume(store, record)
        record.update(state="applying", error=None)
        save(store, record)
        committed = False
        try:
            # The rows go first and together. A file is unlinked only after that commit,
            # so a failure leaves the mirror and the disk telling the same story.
            async with transaction(store.session_factory) as session:
                await session.execute(sa.text("SELECT 1"))
                # Recheck the filesystem after the DB await, under the exclusive gate.
                if (
                    not resuming
                    and record["retention_days"] != store.control.settings().retention.days
                ):
                    raise PurgeChanged()
                disk_ids, cited, _ = await asyncio.to_thread(disk_references, store)
                live_artifacts = set((await session.scalars(sa.select(Artifact.id))).all())
                if any(
                    (
                        c["candidate"]["id"] in disk_ids
                        if c["candidate"]["kind"] == "note"
                        else c["candidate"]["id"] in live_artifacts
                        if c["candidate"]["kind"] == "artifact"
                        else c["candidate"]["id"] in cited or c["opening"] in disk_ids
                    )
                    for c in record["items"]
                ):
                    raise PurgeChanged()
                now = datetime.now(UTC)
                for item in record["items"]:
                    c = item["candidate"]
                    if c["kind"] == "note":
                        await indexer.drop_note(session, c["id"], now)
                        await session.execute(sa.delete(Note).where(Note.id == c["id"]))
                    elif c["kind"] == "artifact":
                        continue
                    else:
                        await indexer.drop_source(session, c["id"], now)
                        await session.execute(sa.delete(Source).where(Source.id == c["id"]))
            committed = True
            for item in record["items"]:
                await asyncio.to_thread(_unlink, store, item)
            record.update(
                state="complete", completed=[i["candidate"]["id"] for i in record["items"]]
            )
            save(store, record)
        except BaseException:
            record.update(
                state="failed" if committed or resuming else "preview",
                error=(
                    "Purge did not finish. Check storage and database health, "
                    "then retry this saved list. Git history is unchanged."
                ),
            )
            save(store, record)
            raise
        await _rescan(store)
        return result(record)


def _present(store: LocalStore, item: dict[str, Any]) -> bool:
    relative = [(_root(store, f["area"]), f["path"]) for f in item["files"]]
    relative += [(_directory_root(store, item), d) for d in item["directories"]]
    return any(os.path.lexists(root / path) for root, path in relative)


async def outcome(store: LocalStore, record: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Which listed ids are gone and which still have a row or a file."""
    removed: list[str] = []
    remaining: list[str] = []
    async with store.session_factory() as session:
        for item in record["items"]:
            c = item["candidate"]
            model = {"note": Note, "source": Source, "artifact": Artifact}[c["kind"]]
            row = await session.get(model, c["id"])
            held = row is not None or await asyncio.to_thread(_present, store, item)
            (remaining if held else removed).append(c["id"])
    return removed, remaining


def _quarantine(store: LocalStore, path: Path) -> PurgeResult:
    destination = directory(store) / QUARANTINE / path.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(path, destination)
    sync_directory(destination.parent)
    sync_directory(path.parent)
    now = datetime.now(UTC)
    return PurgeResult(
        token=path.stem,
        state="abandoned",
        created_at=now,
        retention_days=store.control.settings().retention.days,
        candidates=[],
        counts=PurgeCounts(),
    )


async def abandon(store: LocalStore, request: AbandonPurge) -> PurgeResult:
    async with store.maintenance.exclusive():
        path = _locate(store, request.token)
        record = _load(path)
        if record is None:
            quarantined = _quarantine(store, path)
            await _rescan(store)
            return quarantined
        if record["state"] not in {"applying", "failed"}:
            raise ValidationFailed(["Only an unfinished purge can be abandoned."])
        removed, remaining = await outcome(store, record)
        record.update(state="abandoned", completed=removed, remaining=remaining)
        save(store, record)
        await _rescan(store)
        return result(record)


async def operation(
    store: LocalStore, request: ConfirmPurge | AbandonPurge | None = None
) -> PurgeResult:
    try:
        if request is None:
            return await preview(store)
        if isinstance(request, AbandonPurge):
            return await abandon(store, request)
        return await confirm(store, request)
    except SQLAlchemyError as exc:
        if request is None:
            preview_failure(store)
        raise MetadataUnavailable("purge metadata unavailable") from exc
    except (OSError, ValueError, fm.FrontmatterError) as exc:
        if request is None:
            preview_failure(store)
        message = "Purge could not safely read or remove files. Inspect Problems and retry."
        filename = getattr(exc, "filename", None)
        if filename and Path(str(filename)).is_relative_to(store.sources_root):
            raise SourcesFilesystemUnavailable(message) from exc
        raise NotesFilesystemUnavailable(message) from exc


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
