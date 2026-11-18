"""Artifacts: the second first-class kind beside notes.

An artifact is something static that was handed in or produced: a deck, a
PDF, an image, a link. It is never a note and no path here turns it into one.
Three things hold it, each with one job:

- The archive, `/data/artifacts/<id>/`: `manifest.json` and one `rNNNN/`
  directory per revision holding the bytes under their original filename,
  created exclusively and never opened for writing again. This is the copy
  that survives whatever a device does to the browsable one.
- The browsable copy, `<artifacts folder>/{yyyy}/{yyyy-mm}/<filename>` in the
  notes filesystem, keyed on the received date, so Obsidian shows the file and
  a note links it with an ordinary `[[...]]`. No generated Markdown sits beside
  it. A link artifact has no browsable copy at all.
- The record, `/data/state/artifacts/<id>.json`: provenance plus the text the
  extractor read out of the current revision. It is derived only from the
  archive, regenerated when the extractor changes, mirrored into PostgreSQL
  and the search index, and gone when the artifact goes. It is never a page in
  the notes filesystem.

`rebuild_metadata` recreates every record, row and search entry from the
archive and the browsable copies alone.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from coppermind import frontmatter as fm
from coppermind.atomicio import (
    atomic_write_bytes,
    create_exclusive_bytes,
    replace_staged,
    stage_bytes,
    sync_directory,
)
from coppermind.db.models import Artifact, Note
from coppermind.db.session import transaction
from coppermind.embeds import rewrite_embeds
from coppermind.ids import is_valid_id, new_id
from coppermind.logging import get_logger
from coppermind.naming import MAX_STEM_LENGTH, sanitize_folder, sanitize_stem
from coppermind.settings import ProductSettings
from coppermind.statefiles import RevisionConflict
from coppermind.store_protocol import (
    ArtifactBytes,
    ArtifactMissing,
    ArtifactQuery,
    ArtifactRecord,
    ArtifactRevision,
    ArtifactsFilesystemUnavailable,
    ArtifactSummary,
    ArtifactTextStatus,
    CitedNote,
    CreateArtifact,
    CreatedArtifact,
    MetadataUnavailable,
    NotesFilesystemUnavailable,
    NotFound,
    Page,
    PayloadTooLarge,
    ProblemInfo,
    ReplaceNote,
    StagedReservation,
    StoreError,
    ValidationFailed,
)
from coppermind_store.extraction import EXTRACTOR_VERSION, extract, media_type
from coppermind_store.fs import resolve

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from coppermind_store.notes import LocalStore

log = get_logger("coppermind-store")

MANIFEST_SCHEMA = 1
RECORD_SCHEMA = 1

# Re-extractions one reconciler pass performs after the extractor changed, so
# an upgrade reaches a large archive over successive passes rather than one.
REEXTRACT_PER_PASS = 10
# Legacy attachments one pass moves into the artifacts folder.
MIGRATE_PER_PASS = 50
# Why an archive is marked removed. The owner's own removals, from Admin, from
# an agent through the API, or by deleting the file in Obsidian, make the
# archive eligible for the retention purge; nothing else does.
REMOVED_IN_ADMIN = "removed in Admin"
REMOVED_THROUGH_API = "removed through the API"
REMOVED_AFTER_REVIEW = "recording removed after review"
DELETED_IN_OBSIDIAN = "deleted in Obsidian"
OWNER_REMOVAL_REASONS = frozenset({REMOVED_IN_ADMIN, REMOVED_THROUGH_API, DELETED_IN_OBSIDIAN})
REMOVAL_REASONS = {"admin": REMOVED_IN_ADMIN, "api": REMOVED_THROUGH_API}

_LEGACY_ATTACHMENT = re.compile(
    r"^(?P<source>[0-7][0-9A-HJKMNP-TV-Z]{25})-r(?P<rev>\d{4})-(?P<name>.+)$"
)


# ---------------------------------------------------------------------------
# Paths and files


def records_dir(store: LocalStore) -> Path:
    return store.control.store.state_dir / "artifacts"


def record_path(store: LocalStore, artifact_id: str) -> Path:
    return records_dir(store) / f"{artifact_id}.json"


def manifest_path(store: LocalStore, artifact_id: str) -> Path:
    return store.artifacts_root / artifact_id / "manifest.json"


def _json_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactsFilesystemUnavailable(f"the artifact {label} is unreadable") from exc
    if not isinstance(document, dict):
        raise ArtifactsFilesystemUnavailable(f"the artifact {label} is invalid")
    return document


def read_manifest(store: LocalStore, artifact_id: str) -> dict[str, Any]:
    """The archive manifest, or `ArtifactMissing` when no archive holds the id."""
    if not is_valid_id(artifact_id):
        raise ArtifactMissing(artifact_id)
    try:
        manifest = _read_json(manifest_path(store, artifact_id), "manifest")
    except FileNotFoundError as exc:
        raise ArtifactMissing(artifact_id) from exc
    if manifest.get("artifact_id") != artifact_id or manifest.get("kind") != "artifact":
        raise ArtifactsFilesystemUnavailable("the artifact manifest names a different artifact")
    return manifest


def _write_manifest(store: LocalStore, manifest: dict[str, Any], *, new: bool) -> None:
    path = manifest_path(store, str(manifest["artifact_id"]))
    if new:
        create_exclusive_bytes(path, _json_bytes(manifest))
        return
    staged = stage_bytes(path, _json_bytes(manifest))
    replace_staged(staged, path)
    sync_directory(path.parent)


def _current_revision(manifest: dict[str, Any]) -> dict[str, Any]:
    wanted = manifest.get("current_revision")
    for item in manifest.get("revisions", []):
        if item.get("revision") == wanted:
            return item
    raise ArtifactsFilesystemUnavailable("the artifact manifest has inconsistent revisions")


def _revision_file(store: LocalStore, artifact_id: str, revision: dict[str, Any]) -> Path:
    number = int(revision["revision"])
    return resolve(store.artifacts_root / artifact_id / f"r{number:04d}", str(revision["filename"]))


def current_file(
    store: LocalStore, artifact_id: str, revision: int | None = None
) -> tuple[Path, dict[str, Any]]:
    """The archived file of an artifact's current (or the named) revision, and its record.

    For a reader that streams the file instead of loading it. A removed artifact and a
    link, which has no file, both raise `ArtifactMissing`.
    """
    manifest = read_manifest(store, artifact_id)
    if manifest.get("removed_at"):
        raise ArtifactMissing(artifact_id)
    if revision is None:
        record = _current_revision(manifest)
    else:
        record = next(
            (r for r in manifest.get("revisions", []) if r.get("revision") == revision), {}
        )
    if not record.get("filename"):
        raise ArtifactMissing(artifact_id)
    return _revision_file(store, artifact_id, record), record


READ_CHUNK_BYTES = 1 << 20


def _verify_file(path: Path, sha256: str, size_bytes: int) -> None:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(READ_CHUNK_BYTES):
            size += len(chunk)
            digest.update(chunk)
    if size != size_bytes or digest.hexdigest() != sha256:
        raise ArtifactsFilesystemUnavailable(
            f"the archived artifact failed verification: {path.name}"
        )


async def _read_chunks(path: Path) -> AsyncIterator[bytes]:
    stream = await asyncio.to_thread(path.open, "rb")
    try:
        while chunk := await asyncio.to_thread(stream.read, READ_CHUNK_BYTES):
            yield chunk
    finally:
        stream.close()


def _time(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ArtifactsFilesystemUnavailable(
            f"the artifact manifest has an invalid {field}"
        ) from exc
    if parsed.tzinfo is None:
        raise ArtifactsFilesystemUnavailable(f"the artifact manifest has an invalid {field}")
    return parsed


def _local_zone(settings: ProductSettings) -> ZoneInfo:
    return ZoneInfo(settings.general.timezone)


def _received_at(given: date | None, settings: ProductSettings) -> datetime:
    """The received instant: now, or noon of the stated day in the operator's zone."""
    zone = _local_zone(settings)
    if given is None:
        return datetime.now(tz=zone)
    return datetime.combine(given, time(12, 0), tzinfo=zone)


def browse_folder(received_at: datetime, settings: ProductSettings) -> str:
    """`Artifacts/{yyyy}/{yyyy-mm}` for the received date, in the operator's zone."""
    local = received_at.astimezone(_local_zone(settings))
    root = sanitize_folder(settings.notes.artifacts_folder)
    return f"{root}/{local:%Y}/{local:%Y-%m}"


def artifact_filename(filename: str) -> str:
    """The original filename, portable, with its extension kept."""
    stem, dot, extension = filename.rpartition(".")
    if not dot or not stem:
        stem, extension = filename, ""
    extension = sanitize_stem(extension, fallback="") if extension else ""
    # Reserve room for the extension and the dot inside the stem budget.
    reserve = len(extension) + 1 if extension else 0
    safe_stem = sanitize_stem(stem, fallback="artifact")[: max(MAX_STEM_LENGTH - reserve, 1)]
    safe_stem = safe_stem.rstrip(". ").strip() or "artifact"
    return f"{safe_stem}.{extension}" if extension else safe_stem


def unique_filename(filename: str, taken: set[str]) -> str:
    """`filename`, or the first ` (n)` variant free in the folder, compared case-insensitively."""
    lowered = {name.casefold() for name in taken}
    if filename.casefold() not in lowered:
        return filename
    stem, dot, extension = filename.rpartition(".")
    if not dot or not stem:
        stem, extension = filename, ""
    suffix = 2
    while True:
        candidate = f"{stem} ({suffix}).{extension}" if extension else f"{stem} ({suffix})"
        if candidate.casefold() not in lowered:
            return candidate
        suffix += 1


def _entries(folder: Path) -> set[str]:
    try:
        return {entry.name for entry in folder.iterdir()}
    except FileNotFoundError:
        return set()
    except OSError as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc


def _browse_stat(path: Path) -> dict[str, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


# ---------------------------------------------------------------------------
# Records


def _record_document(
    manifest: dict[str, Any],
    *,
    text: str,
    text_status: ArtifactTextStatus,
    browse: dict[str, int] | None,
    state: str,
) -> dict[str, Any]:
    current = _current_revision(manifest)
    return {
        "schema_version": RECORD_SCHEMA,
        "id": manifest["artifact_id"],
        "kind": "artifact",
        "title": manifest["title"],
        "origin": manifest["origin"],
        "received_at": manifest["received_at"],
        "filename": current.get("filename"),
        "mime_type": current.get("mime_type"),
        "sha256": current.get("sha256"),
        "size_bytes": int(current.get("size_bytes", 0)),
        "url": manifest.get("url"),
        "tags": list(manifest.get("tags", [])),
        "path": manifest.get("path"),
        "revision": int(manifest["current_revision"]),
        "source_id": manifest.get("source_id"),
        "source_name": manifest.get("source_name"),
        "text": text,
        "text_status": text_status,
        "extractor_version": EXTRACTOR_VERSION,
        "extracted_at": datetime.now(tz=UTC).isoformat(),
        "browse": browse,
        "state": state,
        "created_at": manifest["created_at"],
        "updated_at": current["stored_at"],
    }


def write_record(store: LocalStore, document: dict[str, Any]) -> None:
    try:
        atomic_write_bytes(record_path(store, str(document["id"])), _json_bytes(document))
    except OSError as exc:
        raise ArtifactsFilesystemUnavailable(str(exc)) from exc


def read_record(store: LocalStore, artifact_id: str) -> dict[str, Any] | None:
    try:
        return _read_json(record_path(store, artifact_id), "record")
    except FileNotFoundError:
        return None


def record_is_current(document: dict[str, Any] | None, manifest: dict[str, Any]) -> bool:
    """Whether a cached record describes this manifest's revision under this extractor.

    A revision write interrupted between the manifest and the record leaves a
    record naming the previous revision; it is regenerated, never reused.
    """
    return (
        document is not None
        and document.get("extractor_version") == EXTRACTOR_VERSION
        and document.get("revision") == manifest.get("current_revision")
    )


def _extract_record(
    store: LocalStore, manifest: dict[str, Any], *, state: str = "ok"
) -> dict[str, Any]:
    """Derive the record from the current revision's archived bytes."""
    current = _current_revision(manifest)
    if manifest.get("url") and not current.get("filename"):
        text = f"{manifest['title']}\n{manifest['url']}"
        status: ArtifactTextStatus = "link"
    else:
        text, status = extract(
            _revision_file(store, str(manifest["artifact_id"]), current),
            str(current.get("mime_type") or "application/octet-stream"),
        )
    browse = None
    if manifest.get("path"):
        try:
            browse = _browse_stat(resolve(store.notes_root, str(manifest["path"])))
        except ValueError:
            browse = None
    return _record_document(manifest, text=text, text_status=status, browse=browse, state=state)


def _record(
    document: dict[str, Any], manifest: dict[str, Any], citing: list[CitedNote]
) -> ArtifactRecord:
    revisions = [
        ArtifactRevision.model_validate(
            {
                "revision": item["revision"],
                "stored_at": item["stored_at"],
                "filename": item.get("filename"),
                "mime_type": item.get("mime_type"),
                "sha256": item.get("sha256"),
                "size_bytes": item.get("size_bytes", 0),
                "url": item.get("url"),
            }
        )
        for item in manifest.get("revisions", [])
    ]
    return ArtifactRecord.model_validate(
        {
            **document,
            "citing_note_ids": [note.id for note in citing],
            "citing_notes": citing,
            "revisions": revisions,
        }
    )


# ---------------------------------------------------------------------------
# Mirror


def _row_values(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": document["title"],
        "origin": document["origin"],
        "received_at": _time(document["received_at"], "received_at"),
        "filename": document.get("filename"),
        "mime_type": document.get("mime_type"),
        "sha256": document.get("sha256"),
        "size_bytes": int(document.get("size_bytes", 0)),
        "url": document.get("url"),
        "tags": [str(tag) for tag in document.get("tags", [])],
        "path": document.get("path"),
        "current_revision": int(document["revision"]),
        "source_id": document.get("source_id"),
        "source_name": document.get("source_name"),
        "text_status": document.get("text_status", "unsupported"),
        "extractor_version": document.get("extractor_version", ""),
        "state": document.get("state", "ok"),
        "created_at": _time(document["created_at"], "created_at"),
        "updated_at": _time(document["updated_at"], "updated_at"),
    }


async def mirror_record(session: AsyncSession, document: dict[str, Any], now: datetime) -> None:
    """Upsert the row and the search entry for one record, in the caller's transaction."""
    from coppermind_store.indexer import index_artifact

    values = _row_values(document)
    artifact_id = str(document["id"])
    row = await session.get(Artifact, artifact_id)
    if row is None:
        session.add(Artifact(id=artifact_id, **values))
    else:
        for key, value in values.items():
            setattr(row, key, value)
    await session.flush()
    await index_artifact(
        session,
        artifact_id=artifact_id,
        path=document.get("path") or "",
        title=str(document["title"]),
        body=index_body(document),
        version=search_version(int(document["revision"]), str(document["extractor_version"])),
        now=now,
    )


def index_body(document: dict[str, Any]) -> str:
    """What the search index holds for an artifact: its text, its name and its link."""
    parts = [str(document.get("text") or "")]
    if document.get("filename"):
        parts.append(str(document["filename"]))
    if document.get("url"):
        parts.append(str(document["url"]))
    return "\n".join(part for part in parts if part)


def search_version(revision: int, extractor_version: str) -> str:
    return f"r{revision}:{extractor_version}"


async def _remove_rows(session: AsyncSession, artifact_id: str, now: datetime) -> None:
    from coppermind_store.indexer import drop_entry

    await session.execute(sa.delete(Artifact).where(Artifact.id == artifact_id))
    await drop_entry(session, "artifact", artifact_id, now)


async def citing_notes(
    session: AsyncSession, artifact_ids: list[str]
) -> dict[str, list[CitedNote]]:
    """Which live notes cite each of `artifact_ids`, with their titles, from the mirror."""
    if not artifact_ids:
        return {}
    cited = sa.func.unnest(Note.artifact_ids).label("artifact_id")
    statement = (
        sa.select(Note.id, Note.title, Note.path, cited)
        .where(
            Note.artifact_ids.overlap(artifact_ids),
            Note.state.not_in(("missing", "deleted")),
        )
        .order_by(Note.id)
    )
    by_artifact: dict[str, list[CitedNote]] = {artifact_id: [] for artifact_id in artifact_ids}
    for note_id, title, path, artifact_id in (await session.execute(statement)).all():
        if artifact_id in by_artifact and all(n.id != note_id for n in by_artifact[artifact_id]):
            by_artifact[artifact_id].append(
                CitedNote(id=note_id, title=title or Path(path).stem, path=path)
            )
    return by_artifact


async def known_paths(store: LocalStore, artifact_ids: list[str]) -> dict[str, str | None]:
    """The browsable path of each known artifact; `ValidationFailed` names any unknown one."""
    wanted = sorted({value for value in artifact_ids if isinstance(value, str)})
    if not wanted:
        return {}
    try:
        async with store.session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(Artifact.id, Artifact.path).where(Artifact.id.in_(wanted))
                )
            ).all()
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
    found = {row.id: row.path for row in rows}
    unknown = [value for value in wanted if value not in found]
    if unknown:
        key = store.control.schema().optional_role("artifacts_key")
        raise ValidationFailed([f"{key}: no artifact with id {value}" for value in unknown])
    return found


def link_for(path: str | None, title: str, url: str | None) -> str:
    """How a note links an artifact: a wikilink to the file, or a Markdown link for a URL."""
    if path:
        return f"[[{path}]]"
    return f"[{title}]({url})" if url else title


def body_with_links(body: str, links: list[str]) -> str:
    """`body` with each link appended once, under its own heading when any is new."""
    missing = [link for link in links if link not in body]
    if not missing:
        return body
    text = body if body.endswith("\n") or not body else body + "\n"
    if "## Artifacts" not in text:
        text += "\n## Artifacts\n\n"
    return text + "\n".join(missing) + "\n"


# ---------------------------------------------------------------------------
# Writes


def _size_limit(store: LocalStore, size: int, payload_size_bytes: int | None) -> None:
    limit = store.control.settings().limits.artifact_max_bytes
    if max(size, payload_size_bytes or 0) > limit:
        raise PayloadTooLarge(limit)


async def create_artifact(
    store: LocalStore, request: CreateArtifact, *, payload_size_bytes: int | None = None
) -> CreatedArtifact:
    """Store a file or a link as a new artifact, a new revision, or a replay."""
    data: bytes | None = None
    mime: str | None = None
    filename: str | None = None
    sha: str | None = None
    if request.content_base64 is not None:
        try:
            data = request.bytes()
        except (binascii.Error, ValueError) as exc:
            raise ValidationFailed(["content_base64 is not valid base64"]) from exc
        _size_limit(store, len(data), payload_size_bytes)
        filename = artifact_filename(cast(str, request.filename))
        mime = media_type(filename, request.mime_type, data)
        sha = hashlib.sha256(data).hexdigest()
    return await _file_artifact(
        store,
        title=request.title or (Path(filename).stem if filename else cast(str, request.url)),
        origin=request.origin,
        received=request.date,
        tags=request.tags,
        note_id=request.note_id,
        artifact_id=request.artifact_id,
        url=request.url,
        filename=filename,
        mime_type=mime,
        content=data,
        sha256=sha,
    )


async def create_staged_artifact(
    store: LocalStore, staged: Path, declaration: StagedReservation, *, sha256: str
) -> CreatedArtifact:
    """Store a staged file as an artifact by the same path an inline upload takes.

    The file is copied into the archive and the browsable copy in fixed-size
    chunks, never read whole. The caller has verified `sha256` against it.
    """
    filename = artifact_filename(declaration.filename)
    fields = declaration.fields
    with staged.open("rb") as stream:
        head = stream.read(8)
    return await _file_artifact(
        store,
        title=fields.title or Path(filename).stem,
        origin=fields.origin,
        received=fields.date,
        tags=fields.tags,
        note_id=fields.note_id,
        artifact_id=fields.artifact_id,
        url=None,
        filename=filename,
        mime_type=media_type(filename, fields.mime_type, head),
        content=staged,
        sha256=sha256,
    )


async def _file_artifact(
    store: LocalStore,
    *,
    title: str,
    origin: str,
    received: date | None,
    tags: list[str],
    note_id: str | None,
    artifact_id: str | None,
    url: str | None,
    filename: str | None,
    mime_type: str | None,
    content: bytes | Path | None,
    sha256: str | None,
) -> CreatedArtifact:
    settings = store.control.settings()
    if note_id is not None:
        # The citing note must exist before anything is written: a typo in a
        # note id is refused rather than storing an artifact nothing cites.
        await store._locate(note_id)
    async with store._artifact_lock:
        result = await _store_revision(
            store,
            settings,
            artifact_id=artifact_id,
            title=title,
            origin=origin,
            received_at=_received_at(received, settings),
            tags=list(tags),
            url=url,
            filename=filename,
            mime_type=mime_type,
            data=content,
            sha256=sha256,
            source=None,
        )
    if note_id is not None:
        await cite(store, note_id, [result.id])
        result = result.model_copy(update={"note_id": note_id})
    return result


async def file_source_artifact(
    store: LocalStore,
    *,
    source_id: str,
    source_name: str,
    provider: str,
    mime_type: str,
    data: bytes | Path,
    received_at: datetime,
) -> tuple[str, str]:
    """Register a source's non-text artifact as an artifact and return `(id, path)`.

    The same source artifact always resolves to the same artifact identity, so
    refiling after a source revision adds a revision rather than a second file,
    and the browsable path a note links stays where it is.
    """
    settings = store.control.settings()
    sha = (
        await asyncio.to_thread(_file_sha256, data)
        if isinstance(data, Path)
        else hashlib.sha256(data).hexdigest()
    )
    async with store._artifact_lock:
        try:
            async with store.session_factory() as session:
                existing = await session.scalar(
                    sa.select(Artifact.id).where(
                        Artifact.source_id == source_id, Artifact.source_name == source_name
                    )
                )
        except (SQLAlchemyError, OSError) as exc:
            raise MetadataUnavailable(str(exc)) from exc
        if existing is None:
            existing = await asyncio.to_thread(_find_source_manifest, store, source_id, source_name)
        result = await _store_revision(
            store,
            settings,
            artifact_id=existing,
            title=Path(source_name).stem,
            origin=f"source:{provider}",
            received_at=received_at,
            tags=[],
            url=None,
            filename=artifact_filename(source_name),
            mime_type=mime_type.partition(";")[0].strip().lower(),
            data=data,
            sha256=sha,
            source=(source_id, source_name),
        )
    return result.id, cast(str, result.path)


def _find_source_manifest(store: LocalStore, source_id: str, source_name: str) -> str | None:
    """The artifact filed from a source artifact, found from the archive alone."""
    try:
        manifests = sorted(store.artifacts_root.glob("*/manifest.json"))
    except OSError:
        return None
    for path in manifests:
        try:
            manifest = _read_json(path, "manifest")
        except (StoreError, FileNotFoundError):
            continue
        if (
            manifest.get("source_id") == source_id
            and manifest.get("source_name") == source_name
            and not manifest.get("removed_at")
        ):
            return str(manifest.get("artifact_id"))
    return None


async def _store_revision(
    store: LocalStore,
    settings: ProductSettings,
    *,
    artifact_id: str | None,
    title: str,
    origin: str,
    received_at: datetime,
    tags: list[str],
    url: str | None,
    filename: str | None,
    mime_type: str | None,
    data: bytes | Path | None,
    sha256: str | None,
    source: tuple[str, str] | None,
) -> CreatedArtifact:
    """The one write path: decide new, revision or replay, then land it durably.

    Order: the metadata transaction opens first so a database outage refuses
    the write before any file exists; the archive, the browsable copy and the
    record are written; the mirror row and the search entry are issued; the
    commit comes last. A fault before the archive is complete removes what this
    attempt created. A fault after it leaves complete files that the next
    `rebuild_metadata` or a retry resolves, never a duplicate.
    """
    now = datetime.now(tz=UTC)
    try:
        async with transaction(store.session_factory) as session:
            await session.execute(sa.text("SELECT 1"))
            manifest: dict[str, Any] | None = None
            if artifact_id is not None:
                manifest = await asyncio.to_thread(read_manifest, store, artifact_id)
                if manifest.get("removed_at"):
                    raise ArtifactMissing(artifact_id)
            else:
                replay = await _find_replay(session, sha256, filename, url)
                if replay is not None:
                    manifest = await asyncio.to_thread(read_manifest, store, replay)
                    artifact_id = replay
            if manifest is not None:
                current = _current_revision(manifest)
                same = (
                    current.get("sha256") == sha256
                    if sha256 is not None
                    else manifest.get("url") == url
                )
                if same:
                    record = read_record(store, str(manifest["artifact_id"]))
                    if record is None or not record_is_current(record, manifest):
                        record = await asyncio.to_thread(_extract_record, store, manifest)
                        write_record(store, record)
                    await mirror_record(session, record, now)
                    return CreatedArtifact(
                        id=str(manifest["artifact_id"]),
                        revision=int(manifest["current_revision"]),
                        created=False,
                        path=manifest.get("path"),
                        title=str(manifest["title"]),
                    )
                return await _add_revision(
                    store,
                    session,
                    settings,
                    manifest,
                    now=now,
                    filename=filename,
                    mime_type=mime_type,
                    data=data,
                    sha256=sha256,
                    url=url,
                )
            return await _create_new(
                store,
                session,
                settings,
                now=now,
                title=title,
                origin=origin,
                received_at=received_at,
                tags=tags,
                url=url,
                filename=filename,
                mime_type=mime_type,
                data=data,
                sha256=sha256,
                source=source,
            )
    except BaseException as exc:
        from coppermind_store.notes import _metadata_failure

        typed = _metadata_failure(exc)
        if typed is not None:
            raise typed from exc
        raise


async def _find_replay(
    session: AsyncSession, sha256: str | None, filename: str | None, url: str | None
) -> str | None:
    """An existing artifact these same bytes, or this same link, already made."""
    if sha256 is not None:
        statement = sa.select(Artifact.id).where(
            Artifact.sha256 == sha256, Artifact.filename == filename, Artifact.source_id.is_(None)
        )
    else:
        statement = sa.select(Artifact.id).where(Artifact.url == url)
    return await session.scalar(statement.order_by(Artifact.created_at).limit(1))


async def _create_new(
    store: LocalStore,
    session: AsyncSession,
    settings: ProductSettings,
    *,
    now: datetime,
    title: str,
    origin: str,
    received_at: datetime,
    tags: list[str],
    url: str | None,
    filename: str | None,
    mime_type: str | None,
    data: bytes | Path | None,
    sha256: str | None,
    source: tuple[str, str] | None,
) -> CreatedArtifact:
    artifact_id = new_id()
    archive = store.artifacts_root / artifact_id
    browse_relative: str | None = None
    browse_path: Path | None = None
    if filename is not None:
        folder = browse_folder(received_at, settings)
        folder_path = resolve(store.notes_root, folder)
        name = unique_filename(filename, _entries(folder_path))
        browse_relative = f"{folder}/{name}"
        browse_path = folder_path / name
    revision = {
        "revision": 1,
        "stored_at": now.isoformat(),
        "filename": filename,
        "mime_type": mime_type,
        "sha256": sha256,
        "size_bytes": _size_of(data),
        "url": url,
    }
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "artifact_id": artifact_id,
        "kind": "artifact",
        "title": title,
        "origin": origin,
        "received_at": received_at.isoformat(),
        "tags": tags,
        "url": url,
        "source_id": source[0] if source else None,
        "source_name": source[1] if source else None,
        "path": browse_relative,
        "current_revision": 1,
        "revisions": [revision],
        "created_at": now.isoformat(),
    }
    complete = False
    browse_created = False
    try:
        if data is not None and filename is not None:
            (archive / "r0001").mkdir(parents=True, exist_ok=False)
            await asyncio.to_thread(create_exclusive_bytes, archive / "r0001" / filename, data)
        else:
            archive.mkdir(parents=True, exist_ok=False)
        if browse_path is not None and data is not None:
            try:
                await asyncio.to_thread(create_exclusive_bytes, browse_path, data)
            except FileExistsError:
                # Something landed at the name between the listing and now;
                # take the next free one rather than write over it.
                name = unique_filename(cast(str, filename), _entries(browse_path.parent))
                browse_relative = (
                    f"{browse_path.parent.relative_to(store.notes_root).as_posix()}/{name}"
                )
                browse_path = browse_path.parent / name
                manifest["path"] = browse_relative
                await asyncio.to_thread(create_exclusive_bytes, browse_path, data)
            except OSError as exc:
                raise NotesFilesystemUnavailable(str(exc)) from exc
            browse_created = True
        _write_manifest(store, manifest, new=True)
        record = await asyncio.to_thread(_extract_record, store, manifest)
        write_record(store, record)
        await mirror_record(session, record, now)
        complete = True
    except OSError as exc:
        raise ArtifactsFilesystemUnavailable(str(exc)) from exc
    finally:
        if not complete:
            shutil.rmtree(archive, ignore_errors=True)
            record_path(store, artifact_id).unlink(missing_ok=True)
            if browse_created and browse_path is not None:
                browse_path.unlink(missing_ok=True)
    return CreatedArtifact(
        id=artifact_id, revision=1, created=True, path=browse_relative, title=title
    )


async def _add_revision(
    store: LocalStore,
    session: AsyncSession,
    settings: ProductSettings,
    manifest: dict[str, Any],
    *,
    now: datetime,
    filename: str | None,
    mime_type: str | None,
    data: bytes | Path | None,
    sha256: str | None,
    url: str | None,
) -> CreatedArtifact:
    """Append a revision: new bytes in the archive, the browsable copy brought current."""
    artifact_id = str(manifest["artifact_id"])
    number = int(manifest["current_revision"]) + 1
    previous = _current_revision(manifest)
    revision_dir = store.artifacts_root / artifact_id / f"r{number:04d}"
    replacement_started = False
    created_dir = False
    try:
        if data is not None and filename is not None:
            try:
                revision_dir.mkdir(parents=False, exist_ok=False)
            except FileExistsError as exc:
                # Bytes an earlier interrupted write left behind are the
                # operator's to inspect; this attempt created nothing there.
                raise ArtifactsFilesystemUnavailable(
                    f"the artifact archive holds {revision_dir.name}, which its manifest does not "
                    "record; inspect that directory, remove it, then retry"
                ) from exc
            created_dir = True
            await asyncio.to_thread(create_exclusive_bytes, revision_dir / filename, data)
            manifest["path"] = await asyncio.to_thread(
                _bring_browse_current, store, settings, manifest, previous, filename, data
            )
        revisions = list(manifest.get("revisions", []))
        revisions.append(
            {
                "revision": number,
                "stored_at": now.isoformat(),
                "filename": filename,
                "mime_type": mime_type,
                "sha256": sha256,
                "size_bytes": _size_of(data),
                "url": url,
            }
        )
        manifest.update({"current_revision": number, "revisions": revisions})
        if url is not None:
            manifest["url"] = url
        _write_manifest(store, manifest, new=False)
        replacement_started = True
        record = await asyncio.to_thread(_extract_record, store, manifest)
        write_record(store, record)
        await mirror_record(session, record, now)
    except OSError as exc:
        raise ArtifactsFilesystemUnavailable(str(exc)) from exc
    finally:
        if created_dir and not replacement_started:
            shutil.rmtree(revision_dir, ignore_errors=True)
    return CreatedArtifact(
        id=artifact_id,
        revision=number,
        created=True,
        path=manifest.get("path"),
        title=str(manifest["title"]),
    )


def _size_of(data: bytes | Path | None) -> int:
    if data is None:
        return 0
    return data.stat().st_size if isinstance(data, Path) else len(data)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(READ_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _bring_browse_current(
    store: LocalStore,
    settings: ProductSettings,
    manifest: dict[str, Any],
    previous: dict[str, Any],
    filename: str,
    data: bytes | Path,
) -> str:
    """Make the browsable copy the new revision, never writing over a person's edit.

    The copy is replaced in place while it still hashes to the archived bytes
    of the revision it showed, so links to it keep working. A copy a device
    changed is left as it is and the new revision lands at a fresh name beside
    it; a copy that is gone comes back.
    """
    recorded = manifest.get("path")
    received_at = _time(manifest["received_at"], "received_at")
    folder = browse_folder(received_at, settings)
    folder_path = resolve(store.notes_root, folder)
    target: Path | None = None
    if isinstance(recorded, str) and recorded:
        try:
            target = resolve(store.notes_root, recorded)
        except ValueError:
            target = None
    if target is not None and target.is_file():
        if _file_sha256(target) == previous.get("sha256"):
            atomic_write_bytes(target, data)
            return cast(str, recorded)
        log.info("artifact browse copy was edited on a device; new revision placed beside it")
    try:
        if target is not None and not target.exists():
            create_exclusive_bytes(target, data)
            return cast(str, recorded)
    except OSError as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc
    name = unique_filename(filename, _entries(folder_path))
    try:
        create_exclusive_bytes(folder_path / name, data)
    except OSError as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc
    return f"{folder}/{name}"


async def cite(store: LocalStore, note_id: str, artifact_ids: list[str]) -> None:
    """Write the citation into the note's frontmatter and a link into its body.

    Goes through the ordinary conditional replace, so a device edit landing in
    between is refused rather than overwritten, and the mirror learns the
    citation in the same transaction as the file. Filing never moves an
    artifact; this only touches the note.
    """
    schema = store.control.schema()
    key = schema.optional_role("artifacts_key")
    paths = await known_paths(store, artifact_ids)
    manifests = {
        artifact_id: await asyncio.to_thread(read_manifest, store, artifact_id)
        for artifact_id in artifact_ids
    }
    note = await store.get_note(note_id)
    cited = note.frontmatter.get(key)
    current = (
        [value for value in cited if isinstance(value, str)] if isinstance(cited, list) else []
    )
    updated = current + [value for value in artifact_ids if value not in current]
    links = [
        link_for(paths.get(a), str(manifests[a]["title"]), manifests[a].get("url"))
        for a in artifact_ids
    ]
    body = body_with_links(note.body, links)
    frontmatter = {**note.frontmatter, key: updated}
    if frontmatter == note.frontmatter and body == note.body:
        return
    await store.replace_note(
        note_id, ReplaceNote(frontmatter=frontmatter, body=body), note.content_hash
    )


# ---------------------------------------------------------------------------
# Reads


async def get_artifact(store: LocalStore, artifact_id: str) -> ArtifactRecord:
    manifest = await asyncio.to_thread(read_manifest, store, artifact_id)
    if manifest.get("removed_at"):
        raise ArtifactMissing(artifact_id)
    document = read_record(store, artifact_id)
    if document is None:
        raise ArtifactMissing(artifact_id)
    try:
        async with store.session_factory() as session:
            citing = (await citing_notes(session, [artifact_id])).get(artifact_id, [])
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
    return _record(document, manifest, citing)


async def get_artifact_bytes(
    store: LocalStore, artifact_id: str, revision: int | None = None
) -> ArtifactBytes:
    manifest = await asyncio.to_thread(read_manifest, store, artifact_id)
    if manifest.get("removed_at"):
        raise ArtifactMissing(artifact_id)
    wanted = revision if revision is not None else int(manifest["current_revision"])
    item = next((r for r in manifest.get("revisions", []) if r.get("revision") == wanted), None)
    if item is None:
        raise ValidationFailed([f"revision: artifact {artifact_id} has no revision {wanted}"])
    if not item.get("filename"):
        raise ValidationFailed(["this artifact is a link; it has no bytes to serve"])
    try:
        path = _revision_file(store, artifact_id, item)
        await asyncio.to_thread(_verify_file, path, str(item["sha256"]), int(item["size_bytes"]))
    except (OSError, ValueError) as exc:
        raise ArtifactsFilesystemUnavailable("the archived artifact is unreadable") from exc
    return ArtifactBytes(
        id=artifact_id,
        revision=wanted,
        filename=str(item["filename"]),
        mime_type=str(item.get("mime_type") or "application/octet-stream"),
        sha256=str(item["sha256"]),
        size_bytes=int(item["size_bytes"]),
        content=_read_chunks(path),
    )


def _encode_cursor(received_at: datetime, artifact_id: str) -> str:
    payload = json.dumps(
        {"v": 1, "at": received_at.isoformat(), "id": artifact_id}, separators=(",", ":")
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        if (
            not isinstance(payload, dict)
            or payload.get("v") != 1
            or not isinstance(payload.get("id"), str)
            or not isinstance(payload.get("at"), str)
        ):
            raise ValueError
        at = datetime.fromisoformat(payload["at"])
        if at.utcoffset() is None:
            raise ValueError
        return at, payload["id"]
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise ValidationFailed(["cursor: invalid or expired"]) from exc


async def list_artifacts(store: LocalStore, query: ArtifactQuery) -> Page[ArtifactSummary]:
    """Page the mirrored artifacts, newest received first, with their citing notes."""
    statement = sa.select(Artifact).order_by(Artifact.received_at.desc(), Artifact.id.desc())
    if query.tag is not None:
        statement = statement.where(sa.literal(query.tag) == sa.func.any(Artifact.tags))
    if query.origin is not None:
        statement = statement.where(Artifact.origin == query.origin)
    if query.note_id is not None:
        cites = sa.exists(
            sa.select(Note.id).where(
                Note.id == query.note_id, Artifact.id == sa.func.any(Note.artifact_ids)
            )
        )
        statement = statement.where(cites)
    zone = store.control.settings().general.timezone
    received_day = sa.cast(sa.func.timezone(zone, Artifact.received_at), sa.Date)
    if query.from_date is not None:
        statement = statement.where(received_day >= query.from_date)
    if query.to_date is not None:
        statement = statement.where(received_day <= query.to_date)
    if query.cursor:
        at, after = _decode_cursor(query.cursor)
        statement = statement.where(
            sa.or_(
                Artifact.received_at < at,
                sa.and_(Artifact.received_at == at, Artifact.id < after),
            )
        )
    statement = statement.limit(query.limit + 1)
    try:
        async with store.session_factory() as session:
            rows = list((await session.scalars(statement)).all())
            citing = await citing_notes(session, [row.id for row in rows[: query.limit]])
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
    items = [
        ArtifactSummary(
            id=row.id,
            title=row.title,
            origin=row.origin,
            received_at=row.received_at,
            filename=row.filename,
            mime_type=row.mime_type,
            sha256=row.sha256,
            size_bytes=row.size_bytes,
            url=row.url,
            tags=list(row.tags),
            path=row.path,
            revision=row.current_revision,
            citing_note_ids=[note.id for note in citing.get(row.id, [])],
            citing_notes=citing.get(row.id, []),
            text_status=cast(ArtifactTextStatus, row.text_status),
            state=cast(Any, row.state),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows[: query.limit]
    ]
    next_cursor = (
        _encode_cursor(rows[query.limit - 1].received_at, rows[query.limit - 1].id)
        if len(rows) > query.limit
        else None
    )
    return Page[ArtifactSummary](items=items, next_cursor=next_cursor)


async def count_artifacts(session: AsyncSession) -> int:
    return await session.scalar(sa.select(sa.func.count()).select_from(Artifact)) or 0


async def artifact_problems(store: LocalStore) -> list[ProblemInfo]:
    """Artifacts whose browsable copy changed on a device, or whose text extraction failed."""
    try:
        async with store.session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(
                        Artifact.id,
                        Artifact.path,
                        Artifact.title,
                        Artifact.state,
                        Artifact.text_status,
                    )
                    .where(
                        sa.or_(
                            Artifact.state.in_(("modified", "missing")),
                            Artifact.text_status == "failed",
                        )
                    )
                    .order_by(Artifact.received_at.desc())
                )
            ).all()
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
    problems = []
    for row in rows:
        if row.state == "missing":
            problems.append(
                ProblemInfo(
                    kind="artifact",
                    reference=row.path or row.title,
                    reason=(
                        "The browsable copy is absent from the notes filesystem and Git has not "
                        "recorded its deletion yet. The archive is kept. Once the Git helper "
                        "commits the deletion the artifact is retired as deleted in Obsidian and "
                        "becomes eligible for the retention purge; a copy that comes back clears "
                        "this."
                    ),
                    artifact_id=row.id,
                )
            )
        if row.state == "modified":
            problems.append(
                ProblemInfo(
                    kind="artifact",
                    reference=row.path or row.title,
                    reason=(
                        "The browsable copy no longer matches the archived bytes; the original "
                        "is kept in the archive. Upload the changed file as a new revision, or "
                        "restore the copy from the archive."
                    ),
                    artifact_id=row.id,
                )
            )
        if row.text_status == "failed":
            problems.append(
                ProblemInfo(
                    kind="artifact",
                    reference=row.path or row.title,
                    reason=(
                        "Text could not be extracted, so the artifact is searchable by title "
                        "and filename only. The bytes are stored and served unchanged."
                    ),
                    artifact_id=row.id,
                )
            )
    return problems


# ---------------------------------------------------------------------------
# Removal, sweep and recovery


async def remove_artifact(
    store: LocalStore, artifact_id: str, *, reason: str = REMOVED_IN_ADMIN
) -> None:
    """Take the projection away: the browsable copy, the record, the row and the index.

    The archived bytes stay under their identity, marked removed in the
    manifest with the reason, until the retention purge removes them. A
    browsable copy a device changed is left where it is, because it is no
    longer the bytes this store put there.
    """
    async with store.maintenance.shared(), store._artifact_lock:
        manifest = await asyncio.to_thread(read_manifest, store, artifact_id)
        if manifest.get("removed_at"):
            raise ArtifactMissing(artifact_id)
        await _retire(store, manifest, reason=reason)


async def restore_artifact(store: LocalStore, artifact_id: str) -> str:
    """Bring back an artifact its owner removed, while the archive still holds it.

    The removal mark comes off the manifest, the browsable copy returns to the name it
    had (or to a free name beside whatever took it), and the record, row and search
    entry are rebuilt from the archived bytes. Returns the artifact's title.
    """
    async with store.maintenance.shared(), store._artifact_lock:
        manifest = await asyncio.to_thread(read_manifest, store, artifact_id)
        if not manifest.get("removed_at"):
            return str(manifest.get("title") or "")
        if manifest.get("removed_reason") not in OWNER_REMOVAL_REASONS:
            raise ArtifactMissing(artifact_id)
        current = _current_revision(manifest)
        if current.get("filename"):
            archived = _revision_file(store, artifact_id, current)
            if not archived.exists():
                raise ArtifactMissing(artifact_id)
            await asyncio.to_thread(
                _verify_file, archived, str(current["sha256"]), int(current["size_bytes"])
            )
            settings = store.control.settings()
            folder = browse_folder(_time(manifest["received_at"], "received_at"), settings)
            recorded = manifest.get("path")
            try:
                target = (
                    resolve(store.notes_root, str(recorded))
                    if recorded
                    else resolve(store.notes_root, folder) / str(current["filename"])
                )
            except ValueError as exc:
                raise ArtifactsFilesystemUnavailable(str(exc)) from exc
            if target.exists() and _file_sha256(target) != current.get("sha256"):
                target = target.parent / unique_filename(target.name, _entries(target.parent))
            if not target.exists():
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(create_exclusive_bytes, target, archived)
                except OSError as exc:
                    raise NotesFilesystemUnavailable(str(exc)) from exc
            manifest["path"] = target.relative_to(store.notes_root).as_posix()
        for key in ("removed_at", "removed_reason", "removal_commit"):
            manifest.pop(key, None)
        now = datetime.now(tz=UTC)
        try:
            _write_manifest(store, manifest, new=False)
            record = await asyncio.to_thread(_extract_record, store, manifest)
            write_record(store, record)
        except OSError as exc:
            raise ArtifactsFilesystemUnavailable(str(exc)) from exc
        async with transaction(store.session_factory) as session:
            await mirror_record(session, record, now)
        return str(manifest.get("title") or manifest.get("filename") or "")


async def remove_recording_audio(store: LocalStore, artifact_id: str, revision: int) -> None:
    """Retire a recording and remove its verified archive, safely resumable after a crash.

    A revised or externally modified artifact is deliberately left for inspection.
    Its manifest remains as the receipt, including the original hash and size.
    """
    async with store._artifact_lock:
        manifest = await asyncio.to_thread(read_manifest, store, artifact_id)
        versions = manifest.get("revisions", [])
        if len(versions) != 1 or manifest.get("current_revision") != revision:
            raise ArtifactsFilesystemUnavailable("The recording has changed since it was accepted.")
        item = versions[0]
        archived = _revision_file(store, artifact_id, item)
        removed = manifest.get("removed_at")
        if archived.exists():
            await asyncio.to_thread(
                _verify_file, archived, str(item["sha256"]), int(item["size_bytes"])
            )
        elif not removed:
            raise ArtifactsFilesystemUnavailable("The archived recording is missing.")
        if not removed:
            recorded = manifest.get("path")
            if recorded:
                target = resolve(store.notes_root, str(recorded))
                if target.exists():
                    await asyncio.to_thread(
                        _verify_file, target, str(item["sha256"]), int(item["size_bytes"])
                    )
        # Also repair an interrupted retirement whose manifest was written before
        # its database transaction or record removal completed.
        if not removed or manifest.get("removed_reason") == REMOVED_AFTER_REVIEW:
            await _retire(store, manifest, reason=REMOVED_AFTER_REVIEW)
        if archived.exists():
            archived.unlink()
            sync_directory(archived.parent)


async def _retire(
    store: LocalStore,
    manifest: dict[str, Any],
    *,
    reason: str,
    removal_commit: str | None = None,
) -> None:
    artifact_id = str(manifest["artifact_id"])
    current = _current_revision(manifest)
    now = datetime.now(tz=UTC)
    try:
        async with transaction(store.session_factory) as session:
            await session.execute(sa.text("SELECT 1"))
            recorded = manifest.get("path")
            if isinstance(recorded, str) and recorded:
                try:
                    target = resolve(store.notes_root, recorded)
                    if target.is_file() and _file_sha256(target) == current.get("sha256"):
                        target.unlink()
                        sync_directory(target.parent)
                except ValueError:
                    pass
                except OSError as exc:
                    raise NotesFilesystemUnavailable(str(exc)) from exc
            manifest["removed_at"] = now.isoformat()
            manifest["removed_reason"] = reason
            if removal_commit is not None:
                manifest["removal_commit"] = removal_commit
            try:
                _write_manifest(store, manifest, new=False)
                record_path(store, artifact_id).unlink(missing_ok=True)
            except OSError as exc:
                raise ArtifactsFilesystemUnavailable(str(exc)) from exc
            await _remove_rows(session, artifact_id, now)
    except BaseException as exc:
        from coppermind_store.notes import _metadata_failure

        typed = _metadata_failure(exc)
        if typed is not None:
            raise typed from exc
        raise
    # Pending voice data follows the recording when it is removed by any route.
    for job in store.context.recordings.jobs():
        if job.artifact_id == artifact_id:
            (store.context.root / "voice-pending" / (job.id + ".json")).unlink(missing_ok=True)


async def sweep(store: LocalStore, *, full: bool = False) -> dict[str, int]:
    """What one reconciler pass does for artifacts, after the notes converged.

    A browsable copy observed gone is read as the owner deleting the artifact
    in Obsidian only once Git records that deletion: the artifact is then
    retired, record and search entry with it, and its archive becomes eligible
    for the retention purge. A copy that is merely absent, because a device
    has not synced yet or the deletion is not committed, is reported `missing`
    and nothing else changes, so the original is never lost to a late sync.
    A copy whose size or mtime moved is hashed: one that no longer matches the
    archive is reported `modified`, never rewritten. A record carrying an
    older extractor version, or describing an older revision than the
    manifest, is regenerated, a bounded number per pass. Legacy attachments
    are moved into the artifacts folder.
    """
    counts = {
        "artifacts_removed": 0,
        "artifacts_missing": 0,
        "artifacts_modified": 0,
        "artifacts_reextracted": 0,
    }
    settings = store.control.settings()
    try:
        async with store.session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(
                        Artifact.id,
                        Artifact.path,
                        Artifact.sha256,
                        Artifact.state,
                        Artifact.extractor_version,
                    ).order_by(Artifact.id)
                )
            ).all()
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
    reextracted = 0
    for row in rows:
        async with store._artifact_lock:
            try:
                manifest = await asyncio.to_thread(read_manifest, store, row.id)
            except ArtifactMissing:
                # The archive is gone from under the row: nothing durable backs
                # it, so the mirror and the index stop describing it.
                async with transaction(store.session_factory) as session:
                    await _remove_rows(session, row.id, datetime.now(tz=UTC))
                record_path(store, row.id).unlink(missing_ok=True)
                counts["artifacts_removed"] += 1
                continue
            if manifest.get("removed_at"):
                async with transaction(store.session_factory) as session:
                    await _remove_rows(session, row.id, datetime.now(tz=UTC))
                record_path(store, row.id).unlink(missing_ok=True)
                counts["artifacts_removed"] += 1
                continue
            document = read_record(store, row.id)
            if not record_is_current(document, manifest):
                if document is not None and reextracted >= REEXTRACT_PER_PASS:
                    continue
                document = await asyncio.to_thread(
                    _extract_record, store, manifest, state=str(row.state or "ok")
                )
                write_record(store, document)
                async with transaction(store.session_factory) as session:
                    await mirror_record(session, document, datetime.now(tz=UTC))
                reextracted += 1
                counts["artifacts_reextracted"] += 1
            assert document is not None
            if row.path:
                outcome = await asyncio.to_thread(_observe_browse, store, manifest, document, full)
                if outcome == "gone":
                    elsewhere = {r.path for r in rows if r.id != row.id and r.path}
                    moved = await asyncio.to_thread(_restore_moved_copy, store, manifest, elsewhere)
                    if moved == "restored":
                        outcome = "ok"
                    elif moved == "unknown":
                        outcome = "missing"
                    else:
                        commit = await asyncio.to_thread(
                            store.note_history.removal_commit,
                            str(manifest["path"]),
                            settings.git.enabled,
                            _time(_current_revision(manifest)["stored_at"], "stored_at"),
                        )
                        if commit is not None:
                            await _retire(
                                store, manifest, reason=DELETED_IN_OBSIDIAN, removal_commit=commit
                            )
                            counts["artifacts_removed"] += 1
                            continue
                        outcome = "missing"
                if outcome in {"missing", "modified", "ok"} and outcome != document.get(
                    "state", "ok"
                ):
                    document["state"] = outcome
                    write_record(store, document)
                    async with transaction(store.session_factory) as session:
                        await mirror_record(session, document, datetime.now(tz=UTC))
                    if outcome == "modified":
                        counts["artifacts_modified"] += 1
                    elif outcome == "missing":
                        counts["artifacts_missing"] += 1
    if (store.notes_root / sanitize_folder(settings.notes.attachments_folder)).is_dir():
        counts.update(await migrate_attachments(store))
    return counts


def _restore_moved_copy(store: LocalStore, manifest: dict[str, Any], others: set[str]) -> str:
    """`restored`, `absent` or `unknown` for a copy the owner moved or renamed in the vault.

    Moving or renaming is not deleting: a file under the notes root holding
    exactly the archived bytes, and not another artifact's copy, is put back at
    the path this artifact was filed under, and the artifact is never retired.
    `absent` is answered only after every file was examined; a file that could
    not be read, or a restore that failed, answers `unknown`, which callers
    must not read as a deletion.
    """
    current = _current_revision(manifest)
    unreadable: list[OSError] = []
    complete = True
    try:
        filed = resolve(store.notes_root, str(manifest["path"]))
        size = int(current["size_bytes"])
        placed = _time(current["stored_at"], "stored_at").timestamp()
        dropbox = sanitize_folder(store.context.config().dropbox_folder)
    except (ValueError, KeyError):
        return "unknown"
    for directory, subdirs, names in os.walk(store.notes_root, onerror=unreadable.append):
        subdirs[:] = [
            name
            for name in subdirs
            if not name.startswith(".")
            and (Path(directory) / name).relative_to(store.notes_root).as_posix() != dropbox
        ]
        for name in names:
            found = Path(directory) / name
            relative = found.relative_to(store.notes_root).as_posix()
            if relative in others:
                continue
            try:
                info = found.lstat()
                if stat.S_ISLNK(info.st_mode) or info.st_size != size or info.st_ctime < placed:
                    continue
                matches = _file_sha256(found) == current.get("sha256")
            except OSError:
                complete = False
                continue
            if not matches:
                continue
            try:
                filed.parent.mkdir(parents=True, exist_ok=True)
                create_exclusive_bytes(filed, found)
            except OSError:
                return "unknown"
            try:
                found.unlink()
                sync_directory(filed.parent)
                sync_directory(found.parent)
            except OSError:
                log.warning(
                    "artifact restored but the moved copy could not be removed",
                    artifact_id=str(manifest["artifact_id"]),
                    path=relative,
                )
            _record_move_back(store, manifest, relative)
            return "restored"
    return "absent" if complete and not unreadable else "unknown"


def _record_move_back(store: LocalStore, manifest: dict[str, Any], from_path: str) -> None:
    """Leave the owner a visible notice that a move or rename in the vault was undone.

    The notice sits in the same current-notice list a hand-moved reviewed note
    uses, so Admin's Home, the Artifacts page and `GET /v1/notices` all
    show what moved, from where, where it went back and why. Nothing needs
    doing about it; dismissing it only hides it.
    """
    filed = str(manifest["path"])
    folder = filed.rpartition("/")[0]
    log.warning(
        "artifact moved in the vault and returned to its filed location",
        artifact_id=str(manifest["artifact_id"]),
        from_path=from_path,
        path=filed,
    )
    try:
        store.control.record_reconciler_notice(
            note_id=str(manifest["artifact_id"]),
            title=str(manifest.get("title") or Path(filed).name),
            from_path=from_path,
            path=filed,
            folder=folder,
            reason=(
                f"Moved back to {folder}/ because artifacts are filed by the month they "
                "arrived; a move or rename in Obsidian is not kept. Nothing else changed "
                "and nothing needs doing. To take it out of your notes, delete the file "
                "in Obsidian or remove the artifact."
            ),
            at=datetime.now(tz=UTC),
            kind="artifact",
        )
    except (OSError, ValueError, RevisionConflict) as exc:
        log.warning("artifact move notice could not be recorded", reason=type(exc).__name__)


def _observe_browse(
    store: LocalStore, manifest: dict[str, Any], document: dict[str, Any], full: bool
) -> str:
    """`gone`, `modified` or `ok` for the browsable copy, hashing only when a stat moved."""
    try:
        target = resolve(store.notes_root, str(manifest["path"]))
    except ValueError:
        return "gone"
    stat = _browse_stat(target)
    if stat is None:
        return "gone"
    previous = document.get("browse") or {}
    if not full and previous and previous == stat and document.get("state", "ok") == "ok":
        return "ok"
    current = _current_revision(manifest)
    try:
        matches = _file_sha256(target) == current.get("sha256")
    except OSError:
        return str(document.get("state", "ok"))
    document["browse"] = stat
    return "ok" if matches else "modified"


async def recover_artifacts(store: LocalStore) -> dict[str, int]:
    """Rebuild every record and row from the archive, for `rebuild_metadata`.

    Every archive is verified before any row is written; a damaged archive is an
    error, not something to mirror. An artifact whose browsable copy is absent
    comes back as `missing`, never retired: a rebuild cannot tell a deletion
    from a volume that has not synced yet, and the next sweep retires it only
    once Git records the deletion. A record that is missing or stale is
    re-extracted. The search entries follow from the records in the index
    rebuild that runs after this.
    """
    verified: list[dict[str, Any]] = []
    try:
        paths = sorted(store.artifacts_root.glob("*/manifest.json"))
    except OSError as exc:
        raise ArtifactsFilesystemUnavailable(str(exc)) from exc
    for path in paths:
        if not is_valid_id(path.parent.name) or path.is_symlink() or path.parent.is_symlink():
            raise ValueError("invalid artifact archive path")
        manifest = _read_json(path, "manifest")
        if manifest.get("artifact_id") != path.parent.name:
            raise ValueError("artifact manifest identity mismatch")
        retired = bool(manifest.get("removed_at"))
        for item in manifest.get("revisions", []):
            if not item.get("filename"):
                continue
            archived = _revision_file(store, path.parent.name, item)
            if retired and not archived.exists():
                continue
            _verify_file(archived, str(item["sha256"]), int(item["size_bytes"]))
        verified.append(manifest)
    now = datetime.now(tz=UTC)
    recovered = 0
    missing = 0
    async with store._artifact_lock:
        for manifest in verified:
            artifact_id = str(manifest["artifact_id"])
            if manifest.get("removed_at"):
                async with transaction(store.session_factory) as session:
                    await _remove_rows(session, artifact_id, now)
                record_path(store, artifact_id).unlink(missing_ok=True)
                continue
            document = read_record(store, artifact_id)
            state = str((document or {}).get("state", "ok"))
            if manifest.get("path"):
                try:
                    present = resolve(store.notes_root, str(manifest["path"])).is_file()
                except ValueError:
                    present = False
                if not present:
                    state = "missing"
                    missing += 1
                elif state == "missing":
                    state = "ok"
            if not record_is_current(document, manifest):
                document = await asyncio.to_thread(_extract_record, store, manifest, state=state)
                write_record(store, document)
            elif document is not None and document.get("state") != state:
                document["state"] = state
                write_record(store, document)
            assert document is not None
            async with transaction(store.session_factory) as session:
                await mirror_record(session, document, now)
            recovered += 1
        # Rows for archives that no longer exist describe nothing durable.
        async with transaction(store.session_factory) as session:
            known = [str(m["artifact_id"]) for m in verified]
            statement = sa.select(Artifact.id)
            if known:
                statement = statement.where(Artifact.id.not_in(known))
            for artifact_id in (await session.scalars(statement)).all():
                await _remove_rows(session, artifact_id, now)
    return {"recovered_artifacts": recovered, "missing_artifact_copies": missing}


async def migrate_attachments(store: LocalStore) -> dict[str, int]:
    """Move legacy `_Attachments/YYYY-MM/<source>-rNNNN-<name>` files into artifacts.

    Each file becomes the artifact of the source artifact it was copied from,
    keyed on the source revision's ingest date, and the notes embedding the old
    path are repointed and gain the citation. Bounded per pass; the folder is
    removed once it is empty.
    """
    from coppermind_store.sources import _manifest_document

    settings = store.control.settings()
    legacy_root = store.notes_root / sanitize_folder(settings.notes.attachments_folder)
    moved = 0
    repointed: list[tuple[str, str, str, str]] = []
    try:
        files = sorted(p for p in legacy_root.rglob("*") if p.is_file())
    except OSError as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc
    legacy = [(p, m) for p in files if (m := _LEGACY_ATTACHMENT.match(p.name)) is not None]
    registered: list[Path] = []
    for path, match in legacy[:MIGRATE_PER_PASS]:
        source_id = match.group("source")
        revision_number = int(match.group("rev"))
        name = match.group("name")
        try:
            manifest = _manifest_document(store.sources_root / source_id / "manifest.json")
            revision = next(
                item for item in manifest["revisions"] if item["revision"] == revision_number
            )
            artifact = next(item for item in revision["artifacts"] if item["name"] == name)
        except (StoreError, OSError, KeyError, StopIteration, TypeError):
            log.warning("legacy attachment left in place: no source names it", path=path.name)
            continue
        received_at = datetime.fromisoformat(revision["ingested_at"])
        try:
            artifact_id, new_path = await file_source_artifact(
                store,
                source_id=source_id,
                source_name=name,
                provider=str(manifest.get("provider", "")),
                mime_type=str(artifact.get("mime_type", "application/octet-stream")),
                data=path,
                received_at=received_at,
            )
        except OSError as exc:
            raise NotesFilesystemUnavailable(str(exc)) from exc
        old_relative = path.relative_to(store.notes_root).as_posix()
        repointed.append((source_id, name, new_path, old_relative))
        registered.append(path)
    # Every reference moves before any input goes: a repoint that fails leaves
    # the legacy files for the next pass, which registers the same artifacts
    # again as replays and tries the references again.
    if repointed:
        await _repoint_notes(store, repointed, settings)
    for path in registered:
        try:
            path.unlink()
            sync_directory(path.parent)
        except OSError as exc:
            raise NotesFilesystemUnavailable(str(exc)) from exc
        moved += 1
        log.info(
            "legacy attachment moved into the artifacts folder",
            path=path.relative_to(store.notes_root).as_posix(),
        )
    for folder in sorted((p for p in legacy_root.rglob("*") if p.is_dir()), reverse=True):
        with contextlib.suppress(OSError):
            folder.rmdir()
    with contextlib.suppress(OSError):
        legacy_root.rmdir()
    return {"attachments_migrated": moved}


async def _repoint_notes(
    store: LocalStore, repointed: list[tuple[str, str, str, str]], settings: ProductSettings
) -> None:
    """Repoint every link to a moved attachment, in notes and in generated source pages.

    A note goes through the ordinary conditional replace and gains the
    citation; a generated page under the sources folder is this store's own
    output and is rewritten in place.
    """
    schema = store.control.schema()
    id_key = schema.role("id_key")
    artifacts_key = schema.optional_role("artifacts_key")
    legacy = sanitize_folder(settings.notes.attachments_folder)
    embeds = [(source_id, name, new_path) for source_id, name, new_path, _ in repointed]
    try:
        async with store.session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(Artifact.id, Artifact.path).where(
                        Artifact.path.in_([item[2] for item in repointed])
                    )
                )
            ).all()
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
    ids_by_path = {row.path: row.id for row in rows}

    def repoint(body: str) -> str:
        text = rewrite_embeds(body, embeds, attachments_folder=legacy)
        for _, _, new_path, old_relative in repointed:
            text = text.replace(old_relative, new_path)
        return text

    for note_path in sorted(store.notes_root.rglob("*.md")):
        parts = note_path.relative_to(store.notes_root).parts
        if any(part.startswith(".") for part in parts):
            continue
        try:
            text = note_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if f"{legacy}/" not in text:
            continue
        try:
            frontmatter, body = fm.parse(text)
        except fm.FrontmatterError:
            continue
        new_body = repoint(body)
        if new_body == body:
            continue
        if frontmatter.get("managed") is True:
            try:
                atomic_write_bytes(note_path, fm.compose(frontmatter, new_body).encode("utf-8"))
            except OSError as exc:
                raise NotesFilesystemUnavailable(str(exc)) from exc
            continue
        note_id = frontmatter.get(id_key)
        if not is_valid_id(note_id):
            continue
        try:
            note = await store.get_note(str(note_id))
        except NotFound:
            continue
        cited = note.frontmatter.get(artifacts_key)
        current = [v for v in cited if isinstance(v, str)] if isinstance(cited, list) else []
        added = [
            ids_by_path[new_path]
            for _, _, new_path, _ in repointed
            if new_path in ids_by_path and new_path in new_body
        ]
        new_frontmatter = {**note.frontmatter}
        merged = current + [a for a in added if a not in current]
        if merged:
            new_frontmatter[artifacts_key] = merged
        await store.replace_note(
            str(note_id), ReplaceNote(frontmatter=new_frontmatter, body=new_body), note.content_hash
        )
