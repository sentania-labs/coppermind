"""Creating, reading and changing notes: the local implementation of the store contract.

Write protocol, in this order, for every mutation:

1. Validate against the frontmatter schema and choose the target path.
2. Open the metadata transaction. A PostgreSQL outage stops here, before
   anything is written, so a write returns a clean 503 and leaves no orphan
   file behind.
3. Write the file (exclusively for a create, atomically over the old bytes for
   a replace), fsync it, and hash the bytes that landed.
4. Upsert the metadata row and commit. A replace issues its row update before
   step 3 so a value PostgreSQL rejects is refused before the file changes;
   the commit still comes after the write.

The commit is last on purpose: a crash between the write and the commit
leaves the file on disk with no row, or with a row describing the previous
bytes, and the filesystem is the truth, so reconciliation brings PostgreSQL up
to date on its next pass. It never leaves a row pointing at a file that does
not exist. A retry after an ambiguous failure can leave a second copy that
reconciliation surfaces.

A replace is conditional: the caller names the ETag it read, and the compare
against the file's current hash happens under a per-note lock immediately
before the write, so a stale client, or one racing a person's edit delivered
by Obsidian Sync, is refused rather than overwriting the newer bytes.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import time
from datetime import UTC, date, datetime
from math import isfinite
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coppermind import frontmatter as fm
from coppermind.api_keys import ApiKeySet
from coppermind.atomicio import commit_staged, create_exclusive_bytes, stage_bytes, sync_directory
from coppermind.db.models import Note, OutboxEvent, SearchDocument
from coppermind.db.session import transaction
from coppermind.ids import is_valid_id, new_id
from coppermind.logging import get_logger
from coppermind.naming import note_stem, sanitize_folder, unique_stem
from coppermind.purge import AbandonPurge, ConfirmPurge, PurgeResult
from coppermind.schema import (
    FrontmatterSchema,
    account_list,
    format_note_time,
    normalize_note_time,
    note_date,
    parse_note_time,
)
from coppermind.settings import ProductSettings
from coppermind.statefiles import RevisionConflict
from coppermind.store_protocol import (
    ArtifactBytes,
    ArtifactId,
    ArtifactQuery,
    ArtifactRecord,
    ArtifactRemoval,
    ArtifactSummary,
    CreateArtifact,
    CreatedArtifact,
    CreateNote,
    ETag,
    FilterCursorConflict,
    FolderItem,
    FolderTree,
    HistoryStatus,
    IngestRequest,
    IngestResult,
    MetadataUnavailable,
    MoveNote,
    NoteDocument,
    NoteId,
    NoteQuery,
    NoteRecovery,
    NotesFilesystemUnavailable,
    NoteSourceInfo,
    NoteState,
    NoteSummary,
    NoteUnparseable,
    NotFound,
    Page,
    PatchFrontmatter,
    PathCollision,
    PreconditionRequired,
    ProblemInfo,
    RebuildMetadataResult,
    RebuildSearchIndexResult,
    ReconcilerNotice,
    RenameNote,
    ReplaceNote,
    RestoreNote,
    SchemaDocument,
    SearchHit,
    SearchIndexStatus,
    SearchQuery,
    SourceArtifactDocument,
    SourceId,
    SourceManifest,
    SourceQuery,
    SourceSummary,
    StatusResponse,
    StoreError,
    TagCount,
    ValidationFailed,
    VersionConflict,
)
from coppermind_store.control import ControlState
from coppermind_store.fs import NOTE_SUFFIX, content_hash, existing_stems, is_note_file, resolve
from coppermind_store.indexer import index_note
from coppermind_store.maintenance import writer

log = get_logger("coppermind-store")

# How many mirror rows a listing holds in memory at a time. A selective filter
# still walks the mirror, and this bounds the materialized batch.
_SCAN_BATCH = 200

AdoptionOutcome = Literal["adopted", "changed", "invalid", "collision"]
# The outcome and why it happened. "invalid" alone names five different
# refusals, and the reconciler's log line is the only thing that surfaces a
# file the store has decided not to adopt. The cause is content free by
# construction: a parser category or a schema key name, never note bytes.
AdoptionResult = tuple[AdoptionOutcome, str]


def _move_file(source: Path, target: Path, *, retire: Path | None = None) -> None:
    """Link `source` at `target` durably, refusing an occupied target.

    The link is the reservation: it fails with FileExistsError when anything
    already holds the target, before any existing name changes. Then the old
    name goes: `source` itself, or `retire` when `source` is staged new bytes
    for the note at `retire`. If anything fails after the link while the old
    name is still there, the new link is removed again, because two live
    names for one identity are a state the reconciler deliberately leaves
    alone. Once the old name is gone the new one is the only copy and stays.
    """
    original = retire if retire is not None else source
    target.parent.mkdir(parents=True, exist_ok=True)
    os.link(source, target)
    try:
        sync_directory(target.parent)
        original.unlink()
        if retire is not None:
            source.unlink(missing_ok=True)
        sync_directory(original.parent)
    except BaseException:
        if os.path.lexists(original):
            target.unlink(missing_ok=True)
        raise


def _rename_file(
    note_id: NoteId,
    relative: str,
    path: Path,
    new_path: Path,
    new_relative: str,
    data: bytes,
    schema: FrontmatterSchema,
    if_match: ETag,
) -> None:
    """Write `data` under a new name and retire the old one, at `if_match`.

    The renamed bytes are staged beside the destination and linked into
    place, so the file at the old name is never rewritten: a refused or
    failed rename leaves it byte identical.
    """
    try:
        staged = stage_bytes(new_path, data)
    except OSError as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc
    try:
        _bytes_at(note_id, relative, path, schema, if_match)
        _move_file(staged, new_path, retire=path)
    except FileExistsError as exc:
        raise PathCollision(new_relative) from exc
    except OSError as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc
    finally:
        staged.unlink(missing_ok=True)


def _existing_folder(notes_root: Path, folder: str) -> str:
    """`folder` spelled the way the notes filesystem already spells it.

    Windows and macOS compare names case insensitively, so a "work" beside an
    existing "Work" would collide on every such device. Each segment that
    matches an existing folder that way takes that folder's spelling.
    """
    current = notes_root
    resolved: list[str] = []
    for part in folder.split("/"):
        name = part
        try:
            if current.is_dir() and not (current / part).is_dir():
                for entry in sorted(current.iterdir()):
                    if entry.is_dir() and entry.name.casefold() == part.casefold():
                        name = entry.name
                        break
        except OSError as exc:
            raise NotesFilesystemUnavailable(str(exc)) from exc
        resolved.append(name)
        current = current / name
    return "/".join(resolved)


def _renamed_bytes(current: bytes, title: str) -> tuple[bytes, str]:
    """Change the first H1 while preserving the original frontmatter bytes."""
    text = current.decode("utf-8")
    _, body = fm.split(text)
    prefix = text[: len(text) - len(body)]
    lines = body.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("# "):
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines[index] = f"# {title}{ending}"
            break
    else:
        lines.insert(0, f"# {title}\n\n")
    new_body = "".join(lines)
    return (prefix + new_body).encode("utf-8"), new_body


class LocalStore:
    """The store contract, implemented against a real notes filesystem."""

    def __init__(
        self,
        notes_root: Path,
        control: ControlState,
        session_factory: async_sessionmaker[AsyncSession],
        sources_root: Path,
        calendar_database_url: str | None = None,
        artifacts_root: Path | None = None,
    ) -> None:
        self.notes_root = notes_root
        self.sources_root = sources_root
        # The immutable artifact archive sits beside the source bundles.
        self.artifacts_root = artifacts_root or sources_root.parent / "artifacts"
        self.control = control
        self.session_factory = session_factory
        # One lock per note that has been written through this process. The
        # store is exactly one process, which is what makes an in-process lock
        # a sufficient guard for the compare-and-swap.
        self._locks: dict[str, asyncio.Lock] = {}
        self._source_locks: dict[str, asyncio.Lock] = {}
        from coppermind_store.maintenance import MaintenanceGate

        self.maintenance = MaintenanceGate()
        self._filing_lock = asyncio.Lock()
        self._reconcile_lock = asyncio.Lock()
        self._identity_index = _IdentityIndex(self)
        # Every artifact write, whatever its origin, goes through this one lock,
        # which is what makes a replay check and the write it guards one step.
        self._artifact_lock = asyncio.Lock()
        from coppermind_store.note_history import GitHistory

        self.note_history = GitHistory(notes_root)
        from coppermind_store.context_service import ContextService

        self.context = ContextService(self, calendar_database_url)
        from coppermind_store.field_index import FieldIndex

        self.field_index = FieldIndex(self)
        # The one search index rebuild this process may be running.
        self._search_rebuild: asyncio.Task[None] | None = None

    async def get_note_history_status(self) -> HistoryStatus:
        async with self._reconcile_lock:
            await asyncio.to_thread(
                self.note_history.refresh,
                self.control.schema().role("id_key"),
                self.control.settings().git.enabled,
            )
            from coppermind_store.note_history import helper_status

            return helper_status(self, self.note_history.status)

    async def preview_note_restore(self, note_id: str) -> NoteRecovery:
        from coppermind_store.note_history import preview

        try:
            return await preview(self, note_id)
        except BaseException as exc:
            typed = _metadata_failure(exc)
            if typed is None:
                raise
            raise typed from exc

    @writer
    async def restore_note(self, note_id: str, request: RestoreNote) -> NoteRecovery:
        from coppermind_store.note_history import restore

        try:
            return await restore(self, note_id, request)
        except BaseException as exc:
            typed = _metadata_failure(exc)
            if typed is None:
                raise
            raise typed from exc

    async def get_api_keys(self) -> ApiKeySet:
        """Read API key hashes from filesystem-first control state."""
        return self.control.api_keys()

    @writer
    async def ingest(
        self, request: IngestRequest, *, payload_size_bytes: int | None = None
    ) -> IngestResult:
        from coppermind_store.sources import ingest

        result = await ingest(self, request, payload_size_bytes=payload_size_bytes)
        await self._index_after_ingest(result)
        return result

    async def _index_after_ingest(self, result: IngestResult) -> None:
        """Index the projection and the opening note an ingest wrote.

        The files and the mirror are already committed, so an index that
        cannot follow at once must not turn a stored ingest into a failure;
        the catch-up after the next reconciliation pass indexes both.
        """
        from coppermind_store import indexer

        try:
            if result.projection_path:
                await indexer.index_projection(
                    self, result.source.id, result.source.revision, result.projection_path
                )
            await indexer.catch_up(self)
        except Exception as exc:  # noqa: BLE001 - the ingest itself succeeded
            indexer.log.warning(
                "search index did not follow an ingest", error_type=type(exc).__name__
            )

    async def get_source(self, source_id: SourceId) -> SourceManifest:
        from coppermind_store.sources import get_source

        return await get_source(self, source_id)

    async def get_source_artifact(
        self, source_id: SourceId, revision: int, name: str
    ) -> SourceArtifactDocument:
        from coppermind_store.sources import get_source_artifact

        return await get_source_artifact(self, source_id, revision, name)

    async def list_sources(self, query: SourceQuery) -> Page[SourceSummary]:
        from coppermind_store.sources import list_sources

        return await list_sources(self, query)

    async def get_note_sources(self, note_id: NoteId) -> list[NoteSourceInfo]:
        from coppermind_store.sources import get_note_sources

        return await get_note_sources(self, note_id)

    async def preview_purge(self) -> PurgeResult:
        from coppermind_store.purge import operation

        return await operation(self)

    async def confirm_purge(self, request: ConfirmPurge) -> PurgeResult:
        from coppermind_store.purge import operation

        return await operation(self, request)

    async def abandon_purge(self, request: AbandonPurge) -> PurgeResult:
        from coppermind_store.purge import operation

        return await operation(self, request)

    async def get_problems(self) -> list[ProblemInfo]:
        from coppermind_store.sources import get_problems

        return await get_problems(self)

    async def create_artifact(
        self, request: CreateArtifact, *, payload_size_bytes: int | None = None
    ) -> CreatedArtifact:
        from coppermind_store.artifacts import create_artifact

        return await create_artifact(self, request, payload_size_bytes=payload_size_bytes)

    async def get_artifact(self, artifact_id: ArtifactId) -> ArtifactRecord:
        from coppermind_store.artifacts import get_artifact

        return await get_artifact(self, artifact_id)

    async def get_artifact_bytes(
        self, artifact_id: ArtifactId, revision: int | None = None
    ) -> ArtifactBytes:
        from coppermind_store.artifacts import get_artifact_bytes

        return await get_artifact_bytes(self, artifact_id, revision)

    async def list_artifacts(self, query: ArtifactQuery) -> Page[ArtifactSummary]:
        from coppermind_store.artifacts import list_artifacts

        return await list_artifacts(self, query)

    async def get_artifact_limit(self) -> int:
        return self.control.settings().limits.artifact_max_bytes

    async def remove_artifact(
        self, artifact_id: ArtifactId, removal: ArtifactRemoval = "admin"
    ) -> None:
        from coppermind_store.artifacts import REMOVAL_REASONS, remove_artifact

        await remove_artifact(self, artifact_id, reason=REMOVAL_REASONS[removal])

    async def _artifact_links(
        self, frontmatter: dict[str, Any], current: dict[str, Any] | None = None
    ) -> list[str]:
        """The links a note should carry for the artifacts its frontmatter cites.

        Every newly cited identifier must name a stored artifact; an unknown one
        is a validation failure, so a note never gains a citation to something
        that is not there. An identifier the note already cites is left alone,
        so a note keeps its citation of an artifact that was since removed.
        """
        from coppermind_store.artifacts import known_paths, link_for, read_manifest

        cited = frontmatter.get(self.control.schema().optional_role("artifacts_key"))
        if not isinstance(cited, list) or not cited:
            return []
        held = (
            current.get(self.control.schema().optional_role("artifacts_key")) if current else None
        )
        held_ids = (
            {value for value in held if isinstance(value, str)} if isinstance(held, list) else set()
        )
        ids = [value for value in cited if isinstance(value, str) and value not in held_ids]
        paths = await known_paths(self, ids)
        links = []
        for artifact_id in ids:
            manifest = await asyncio.to_thread(read_manifest, self, artifact_id)
            links.append(
                link_for(paths.get(artifact_id), str(manifest["title"]), manifest.get("url"))
            )
        return links

    async def list_reconciler_notices(self) -> list[ReconcilerNotice]:
        from coppermind_store.sources import list_reconciler_notices

        return await list_reconciler_notices(self)

    async def dismiss_reconciler_notice(self, note_id: NoteId) -> None:
        try:
            self.control.clear_reconciler_notice(note_id)
        except RevisionConflict as exc:
            raise MetadataUnavailable(str(exc)) from exc

    async def get_status(self) -> StatusResponse:
        from coppermind_store.sources import get_status

        return await get_status(self)

    @writer
    async def create_note(self, request: CreateNote) -> NoteDocument:
        settings = self.control.settings()
        schema = self.control.schema()

        note_id = new_id()
        frontmatter = _build_frontmatter(request, schema, settings, note_id)
        problems = schema.validate_frontmatter(frontmatter) + schema.tag_problems(frontmatter)
        if problems:
            raise ValidationFailed(problems)
        sources = frontmatter.get(schema.role("sources_key"), [])
        from coppermind_store.artifacts import body_with_links

        composed_body = body_with_links(
            _body_with_heading(request.title, request.body),
            await self._artifact_links(frontmatter),
        )

        if request.folder is not None:
            for seg in request.folder.split("/"):
                if seg == "..":
                    msg = f"folder: path escapes the notes filesystem: {request.folder}"
                    raise ValidationFailed([msg])
            folder = sanitize_folder(request.folder)
            if not folder:
                raise ValidationFailed(["folder: must be a non-empty path"])
            _check_folder_safe(folder, settings)
            try:
                folder_path = resolve(self.notes_root, folder)
            except ValueError as exc:
                msg = f"folder: path escapes the notes filesystem: {request.folder}"
                raise ValidationFailed([msg]) from exc
        else:
            folder = sanitize_folder(settings.notes.review_folder)
            folder_path = resolve(self.notes_root, folder)
        stem = _stem_for(request.title, frontmatter, schema, settings)
        base_stem = stem
        stem = unique_stem(stem, existing_stems(folder_path))
        relative = f"{folder}/{stem}{NOTE_SUFFIX}" if folder else f"{stem}{NOTE_SUFFIX}"
        target = folder_path / f"{stem}{NOTE_SUFFIX}"

        text = fm.compose(frontmatter, composed_body)
        data = text.encode("utf-8")

        created = False
        try:
            async with transaction(self.session_factory) as session:
                # Force the connection before touching the filesystem. SQLAlchemy
                # connects lazily, so without this a PostgreSQL outage would only
                # surface at commit, after the file had already been created.
                await session.execute(sa.text("SELECT 1"))
                # Try to create the file, retrying the stem if another process
                # won the exclusive-create race.  Five attempts lets a burst
                # of concurrent creates all land without one 409ing the others.
                stem_attempts = 0
                while stem_attempts < 5:
                    stem_attempts += 1
                    relative = f"{folder}/{stem}{NOTE_SUFFIX}" if folder else f"{stem}{NOTE_SUFFIX}"
                    target = folder_path / f"{stem}{NOTE_SUFFIX}"
                    # Historical paths are not unique, so this asks whether any
                    # live row holds the path rather than asserting only one
                    # can.
                    occupied = (
                        (
                            await session.execute(
                                sa.select(Note.id)
                                .where(
                                    Note.path == relative, Note.state.not_in(("missing", "deleted"))
                                )
                                .limit(1)
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if occupied is not None:
                        raise PathCollision(relative)
                    try:
                        create_exclusive_bytes(target, data)
                        break
                    except FileExistsError:
                        # Another process created a file at this path between
                        # the listing and the create. Recompute the stem from
                        # the live directory listing and try again with the
                        # next free suffix.
                        stem = unique_stem(base_stem, existing_stems(folder_path))
                        continue
                    except OSError as exc:
                        raise NotesFilesystemUnavailable(str(exc)) from exc
                else:
                    raise PathCollision(relative)

                created = True

                now = datetime.now(tz=UTC)
                digest = content_hash(data)
                session.add(
                    Note(
                        id=note_id,
                        path=relative,
                        title=request.title,
                        content_hash=digest,
                        size_bytes=len(data),
                        mtime=now,
                        frontmatter=_jsonable(frontmatter),
                        **_mirror_columns(frontmatter, schema),
                        state="ok",
                        first_seen_at=now,
                        updated_at=now,
                    )
                )
                await index_note(
                    session,
                    note_id=note_id,
                    path=relative,
                    title=request.title,
                    body=composed_body,
                    version=digest,
                    now=now,
                    notes_folder=settings.notes.notes_folder,
                )
        except BaseException as exc:
            typed = _metadata_failure(exc)
            if typed is None:
                raise
            if isinstance(typed, ValidationFailed) and created:
                target.unlink(missing_ok=True)
            raise typed from exc

        return NoteDocument(
            id=note_id,
            path=relative,
            title=request.title,
            frontmatter=_jsonable(frontmatter),
            body=composed_body,
            content_hash=digest,
            size_bytes=len(data),
            updated_at=now,
            sources=[str(source) for source in sources] if isinstance(sources, list) else [],
        )

    async def get_note(self, note_id: NoteId) -> NoteDocument:
        return await self._with_filing_state(await self._read_current(note_id))

    async def _read_current(self, note_id: NoteId) -> NoteDocument:
        try:
            return await self._read_located(note_id, inline=False)
        except _IdentityStale:
            await self._identity_index.refresh()
        return await self._read_located(note_id, inline=True)

    async def _read_located(self, note_id: NoteId, *, inline: bool) -> NoteDocument:
        async with self._lock_for(note_id):
            relative, path = await self._locate(note_id, inline)
            data, mtime = _read(note_id, path)
            schema = self.control.schema()
            try:
                frontmatter, body = _parse(note_id, relative, data, schema)
            except NotFound:
                matches = await self._followed(note_id, inline)
                if len(matches) != 1:
                    raise
                path = matches[0]
                previous = relative
                relative = path.relative_to(self.notes_root).as_posix()
                await self._repair_location(note_id, previous, relative)
                data, mtime = _read(note_id, path)
                frontmatter, body = _parse(note_id, relative, data, schema)
            sources = frontmatter.get(schema.role("sources_key"), [])
            return NoteDocument(
                id=note_id,
                path=relative,
                title=_title_of(body, path),
                frontmatter=_jsonable(frontmatter),
                body=body,
                content_hash=content_hash(data),
                size_bytes=len(data),
                updated_at=datetime.fromtimestamp(mtime, tz=UTC),
                sources=[str(source) for source in sources] if isinstance(sources, list) else [],
            )

    async def list_notes(self, query: NoteQuery) -> Page[NoteSummary]:
        """List the most recent reconciled state for known note identities.

        PostgreSQL is required because it holds the reconciler's mirror. The
        immutable identifier is both the sort key and cursor, so a move or
        rename between pages cannot change where the next page resumes.

        Rows arrive in keyset-resumed batches rather than as the whole mirror.
        """
        from coppermind_store.field_index import conditions, query_context

        revision = self.control.schema_document().revision
        context = query_context(query, revision) if query.filter else None
        after = _decode_note_cursor(query.cursor, context) if query.cursor else None
        wanted = query.limit + 1
        batch_size = max(wanted, _SCAN_BATCH)
        matched: list[NoteSummary] = []
        exhausted = False
        # The mirror is the first thing to answer, so its own outage keeps its
        # own code; the identity index is the notes filesystem, so its failure
        # keeps the 503 code of that outage instead.
        try:
            while len(matched) < wanted and not exhausted:
                needed = wanted - len(matched)
                candidates: list[tuple[str, NoteSummary]] = []
                async with self.session_factory() as session, session.begin():
                    if query.filter:
                        await session.execute(
                            sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                        )
                    predicates = await conditions(self, query, session)
                    while len(candidates) < needed:
                        statement = (
                            sa.select(Note).where(*predicates).order_by(Note.id).limit(batch_size)
                        )
                        if after is not None:
                            statement = statement.where(Note.id > after)
                        rows = list((await session.scalars(statement)).all())
                        for row in rows:
                            after = row.id
                            summary = _mirrored_summary(row, cast(NoteState, row.state))
                            if _matches_query(summary, query):
                                candidates.append((row.state, summary))
                                if len(candidates) == needed:
                                    break
                        else:
                            if len(rows) < batch_size:
                                exhausted = True
                                break
                from coppermind_store.filing import filing_state
                from coppermind_store.notices import record_unreadable_notice

                # The identity walk answers where a note's file sits. It tells a file
                # that is away now apart from a folder it could not enter: the scan
                # completes and names the identities it reached, while an unreadable
                # folder or missing root makes the whole scan fail, and that is the notes
                # outage. The walk is consulted once a read fails, so a batch whose reads
                # all answer never pays for it.
                scan: dict[str, list[Path]] | None = None
                locations: dict[str, str] = {summary.id: summary.path for _, summary in candidates}
                try:
                    for state, summary in candidates:
                        if state == "ok":
                            try:
                                document = await self._read_current(summary.id)
                            except (NotFound, NoteUnparseable):
                                continue
                            except NotesFilesystemUnavailable:
                                # One file's own failure leaves the root open, so only
                                # that note is left out and the page goes on. A root or
                                # folder the walk could not enter fails the scan itself,
                                # which is the notes outage, and stops the listing.
                                await asyncio.to_thread(self._require_notes_root)
                                if scan is None:
                                    await self._identity_index.refresh()
                                    scan = self._identity_index._found
                                if not scan:
                                    raise
                                if summary.id not in scan:
                                    # The walk reached this file, so the note is away on
                                    # its own: only it is left out and the page goes on.
                                    record_unreadable_notice(self, summary.id, summary.path)
                                    continue
                                raise
                            locations[summary.id] = document.path
                        if locations[summary.id] != summary.path:
                            summary = summary.model_copy(update={"path": locations[summary.id]})
                        for key, value in filing_state(self, summary.id).items():
                            setattr(summary, key, value)
                        matched.append(summary)
                except NotesFilesystemUnavailable:
                    raise
                except (SQLAlchemyError, OSError) as exc:
                    raise MetadataUnavailable(str(exc)) from exc
        except NotesFilesystemUnavailable:
            raise
        except (SQLAlchemyError, OSError) as exc:
            raise MetadataUnavailable(str(exc)) from exc
        if query.filter and self.control.schema_document().revision != revision:
            raise FilterCursorConflict()
        page = matched[: query.limit]
        next_cursor = (
            _encode_note_cursor(page[-1].id, context) if len(matched) > query.limit else None
        )
        return Page[NoteSummary](items=page, next_cursor=next_cursor)

    async def list_tags(self) -> list[TagCount]:
        """Every tag the mirror records on a note still present, with its note count.

        A count is of notes, not of occurrences: a note whose file lists one tag
        twice counts once. A missing note keeps its last known tags in its row,
        so it is left out rather than counted as if it were still there.
        """
        tag = sa.func.unnest(Note.tags).label("tag")
        tagged = sa.select(Note.id, tag).where(Note.state.not_in(("missing", "deleted"))).subquery()
        statement = (
            sa.select(tagged.c.tag, sa.func.count(sa.distinct(tagged.c.id)).label("notes"))
            .group_by(tagged.c.tag)
            .order_by(tagged.c.tag)
        )
        try:
            async with self.session_factory() as session:
                rows = (await session.execute(statement)).all()
        except (SQLAlchemyError, OSError) as exc:
            raise MetadataUnavailable(str(exc)) from exc
        return [TagCount(tag=row.tag, count=row.notes) for row in rows]

    async def get_schema(self) -> SchemaDocument:
        """The schema this store validates writes against, read from its file."""
        try:
            return self.control.schema_document()
        except (OSError, ValueError) as exc:
            raise StoreError(f"the frontmatter schema could not be read: {exc}") from exc

    @writer
    async def replace_note(
        self, note_id: NoteId, request: ReplaceNote, if_match: ETag
    ) -> NoteDocument:
        """Replace a note's frontmatter and body if it still hashes to `if_match`.

        The identifier and the path are kept: a replace never renames or moves.
        The path comes from the mirror, so with PostgreSQL away the write is
        refused before the file is read, let alone written.

        Under the note's lock: check the file is still this note at the ETag
        the caller read, and write back the keys sent unchanged with the types
        that file gives them. Then issue the row update, stage the new bytes
        beside the file, check the file once more and rename the staged bytes
        over it with nothing but that check between the two, and commit last.
        The order keeps the two properties a create has: a value the metadata
        store rejects, or a database that went away, is refused before the
        file changes, and a crash between the rename and the commit leaves the
        file as the truth, with a row that reconciliation brings up to date.
        """
        schema = self.control.schema()
        sent = _replacement_frontmatter(request, schema, note_id)
        problems = schema.validate_frontmatter(sent)
        if problems:
            raise ValidationFailed(problems)
        body = _terminated(request.body)

        relative, path = await self._locate(note_id)
        async with self._lock_for(note_id):
            _, current, _ = _bytes_at(note_id, relative, path, schema, if_match)
            await self._artifact_links(sent, current)
            problems = schema.tag_problems(sent, current)
            if problems:
                raise ValidationFailed(problems)
            frontmatter = _keeping_types(sent, current)
            data = fm.compose(frontmatter, body).encode("utf-8")
            sources = frontmatter.get(schema.role("sources_key"), [])
            now = datetime.now(tz=UTC)
            digest = content_hash(data)
            try:
                async with transaction(self.session_factory) as session:
                    await session.execute(
                        sa.update(Note)
                        .where(Note.id == note_id)
                        .values(
                            title=_title_of(body, path),
                            content_hash=digest,
                            size_bytes=len(data),
                            mtime=now,
                            frontmatter=_jsonable(frontmatter),
                            **_mirror_columns(frontmatter, schema),
                            state="ok",
                            state_reason=None,
                            updated_at=now,
                        )
                    )
                    await index_note(
                        session,
                        note_id=note_id,
                        path=relative,
                        title=_title_of(body, path),
                        body=body,
                        version=digest,
                        now=now,
                        notes_folder=self.control.settings().notes.notes_folder,
                    )
                    _replace_if_unchanged(note_id, relative, path, data, schema, if_match)
                    if current.get(schema.role("reviewed_key")) != frontmatter.get(
                        schema.role("reviewed_key")
                    ):
                        from coppermind_store.filing import record_review

                        record_review(self, note_id, frontmatter)
            except BaseException as exc:
                typed = _metadata_failure(exc)
                if typed is None:
                    raise
                raise typed from exc

        return await self._with_filing_state(
            NoteDocument(
                id=note_id,
                path=relative,
                title=_title_of(body, path),
                frontmatter=_jsonable(frontmatter),
                body=body,
                content_hash=digest,
                size_bytes=len(data),
                updated_at=now,
                sources=[str(source) for source in sources] if isinstance(sources, list) else [],
            )
        )

    @writer
    async def patch_frontmatter(
        self, note_id: NoteId, request: PatchFrontmatter, if_match: ETag
    ) -> NoteDocument:
        """Change only the named frontmatter fields at the caller's version.

        The round-trip YAML mapping retains every untouched line, including a
        person's ordering, comments and the note's list style, so the file
        differs in the one key the caller named whether the note was written
        by Coppermind, by Obsidian or by hand. A targeted change preserves the
        note's content and its ordinary formatting, while some unusual
        formatting is normalised and syncs with it; what survives and what
        does not is recorded shape by shape in
        `coppermind/tests/test_frontmatter.py::test_a_patch_preserves_the_note_and_its_ordinary_formatting`.

        The body comes from the same current file and is never accepted from
        the caller. A patch whose result is byte identical to the file writes
        nothing, so marking an already reviewed note reviewed does not move the
        mtime that Obsidian Sync watches.
        """
        schema = self.control.schema()
        problems = _patch_problems(request, schema)
        if problems:
            raise ValidationFailed(problems)
        changes = _with_kinds(request.set, schema)

        relative, path = await self._locate(note_id)
        async with self._lock_for(note_id):
            current_data, current_frontmatter, current_mtime = _bytes_at(
                note_id, relative, path, schema, if_match
            )
            data = fm.patch(current_data.decode("utf-8"), changes, unset=request.unset).encode(
                "utf-8"
            )
            frontmatter, body = fm.parse(data.decode("utf-8"))

            id_key = schema.role("id_key")
            if frontmatter.get(id_key) != current_frontmatter.get(id_key):
                raise ValidationFailed([f"{id_key}: the identifier of a note cannot be changed"])
            problems = schema.validate_frontmatter(frontmatter)
            problems += schema.tag_problems(frontmatter, current_frontmatter)
            if problems:
                raise ValidationFailed(problems)
            if schema.optional_role("artifacts_key") in request.set:
                await self._artifact_links(frontmatter, current_frontmatter)

            sources = frontmatter.get(schema.role("sources_key"), [])
            now = datetime.now(tz=UTC)
            digest = content_hash(data)
            writes_file = data != current_data
            file_mtime = now if writes_file else datetime.fromtimestamp(current_mtime, tz=UTC)
            try:
                async with transaction(self.session_factory) as session:
                    await session.execute(
                        sa.update(Note)
                        .where(Note.id == note_id)
                        .values(
                            title=_title_of(body, path),
                            content_hash=digest,
                            size_bytes=len(data),
                            mtime=file_mtime,
                            frontmatter=_jsonable(frontmatter),
                            **_mirror_columns(frontmatter, schema),
                            state="ok",
                            state_reason=None,
                            updated_at=now,
                        )
                    )
                    await index_note(
                        session,
                        note_id=note_id,
                        path=relative,
                        title=_title_of(body, path),
                        body=body,
                        version=digest,
                        now=now,
                        notes_folder=self.control.settings().notes.notes_folder,
                    )
                    if writes_file:
                        _replace_if_unchanged(note_id, relative, path, data, schema, if_match)
                    if (
                        schema.role("reviewed_key") in request.set
                        or schema.role("reviewed_key") in request.unset
                    ):
                        from coppermind_store.filing import record_review

                        record_review(self, note_id, frontmatter)
            except BaseException as exc:
                typed = _metadata_failure(exc)
                if typed is None:
                    raise
                raise typed from exc

        return await self._with_filing_state(
            NoteDocument(
                id=note_id,
                path=relative,
                title=_title_of(body, path),
                frontmatter=_jsonable(frontmatter),
                body=body,
                content_hash=digest,
                size_bytes=len(data),
                updated_at=file_mtime,
                sources=[str(source) for source in sources] if isinstance(sources, list) else [],
            )
        )

    @writer
    async def move_note(
        self, note_id: NoteId, request: MoveNote, if_match: ETag | None
    ) -> NoteDocument:
        """Move a note to another folder. The identifier and filename never change.

        The target folder must be inside the notes filesystem root and must not
        start with ``_Sources``; it takes the spelling of an existing folder
        that differs only by case. A stale ``If-Match`` on a move is refused as
        a version conflict; an absent header is accepted because the store
        contract marks it optional. Moving a note to the folder it is already
        in changes nothing.

        The filename is kept as it is, so a " (2)" suffix or a hand-chosen name
        survives and Obsidian links by filename keep resolving; only a rename
        changes it. The mirror row and the outbox event are issued first, the
        file is linked into place and the old name removed, and the commit
        comes last, so the note keeps its id and a reconciler pass sees the
        same note at the new path, not a delete plus a create.
        """
        schema = self.control.schema()
        settings = self.control.settings()

        # Validate the path before sanitizing it. Sanitization must not turn an
        # escaping request into an accepted in-root path.
        raw_folder = request.target_folder.replace("\\", "/")
        raw_parts = raw_folder.split("/")
        if raw_folder.startswith("/") or any(part in {".", ".."} for part in raw_parts):
            raise ValidationFailed(["target_folder: must be a relative folder path"])

        target_folder = sanitize_folder(request.target_folder)
        if not target_folder:
            raise ValidationFailed(["target_folder: must be a non-empty folder name"])
        for reserved in (settings.notes.sources_folder, settings.notes.artifacts_folder):
            reserved_folder = sanitize_folder(reserved).casefold()
            if target_folder.casefold() == reserved_folder or target_folder.casefold().startswith(
                reserved_folder + "/"
            ):
                raise ValidationFailed([f"target_folder: cannot move notes into {reserved}"])
        target_folder = _existing_folder(self.notes_root, target_folder)
        try:
            folder_path = resolve(self.notes_root, target_folder)
        except ValueError as exc:
            raise ValidationFailed(["target_folder: must be a relative folder path"]) from exc

        relative, path = await self._locate(note_id)
        async with self._lock_for(note_id):
            current_data, frontmatter, current_mtime = _bytes_at(
                note_id, relative, path, schema, if_match
            )
            new_relative = f"{target_folder}/{path.name}"
            new_target = folder_path / path.name

            if new_target == path:
                _, body = fm.parse(current_data.decode("utf-8"))
                return await self._with_filing_state(
                    _document(
                        note_id,
                        relative,
                        _title_of(body, path),
                        frontmatter,
                        body,
                        current_data,
                        datetime.fromtimestamp(current_mtime, tz=UTC),
                        schema,
                    )
                )

            if path.stem.casefold() in {name.casefold() for name in existing_stems(folder_path)}:
                raise PathCollision(new_relative)

            now = datetime.now(tz=UTC)
            try:
                async with transaction(self.session_factory) as session:
                    occupied = (
                        (
                            await session.execute(
                                sa.select(Note.id)
                                .where(
                                    Note.path == new_relative,
                                    Note.state.not_in(("missing", "deleted")),
                                )
                                .limit(1)
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if occupied is not None:
                        raise PathCollision(new_relative)

                    current_data, frontmatter, current_mtime = _bytes_at(
                        note_id, relative, path, schema, if_match
                    )
                    _, body = fm.parse(current_data.decode("utf-8"))
                    title = _title_of(body, new_target)

                    # The same identity and its outbox event, in one transaction.
                    await session.execute(
                        sa.update(Note)
                        .where(Note.id == note_id)
                        .values(
                            path=new_relative,
                            title=title,
                            content_hash=content_hash(current_data),
                            size_bytes=len(current_data),
                            frontmatter=_jsonable(frontmatter),
                            **_mirror_columns(frontmatter, schema),
                            mtime=datetime.fromtimestamp(current_mtime, tz=UTC),
                            state="ok",
                            state_reason=None,
                            updated_at=now,
                        )
                    )
                    await index_note(
                        session,
                        note_id=note_id,
                        path=new_relative,
                        title=title,
                        body=body,
                        version=content_hash(current_data),
                        now=now,
                        notes_folder=settings.notes.notes_folder,
                    )
                    session.add(
                        OutboxEvent(
                            event_type="note.moved",
                            note_id=note_id,
                            payload={"from_path": relative, "path": new_relative},
                            created_at=now,
                        )
                    )
                    await session.flush()
                    try:
                        _move_file(path, new_target)
                    except FileExistsError as exc:
                        raise PathCollision(new_relative) from exc
                    except OSError as exc:
                        raise NotesFilesystemUnavailable(str(exc)) from exc
                    self._identity_index.moved(note_id, new_target)
            except BaseException as exc:
                typed = _metadata_failure(exc)
                if typed is None:
                    raise
                raise typed from exc

        return await self._with_filing_state(
            _document(note_id, new_relative, title, frontmatter, body, current_data, now, schema)
        )

    @writer
    async def rename_note(
        self, note_id: NoteId, request: RenameNote, if_match: ETag
    ) -> NoteDocument:
        """Rename a note's filename and first H1 heading.

        The identifier never changes. Links to this note are not rewritten,
        and that gap is deliberate, not a bug. ``If-Match`` is required
        (not optional): renaming is a full document change so the caller must
        state which version it is editing.

        The original frontmatter bytes are preserved unchanged. When the
        filename changes, the renamed bytes are linked in at the new name
        before the old one is removed, so a refused rename leaves the file at
        the old name byte identical.
        """
        if if_match is None:
            raise PreconditionRequired()

        schema = self.control.schema()
        settings = self.control.settings()
        new_title = request.title

        relative, path = await self._locate(note_id)
        async with self._lock_for(note_id):
            current_data, frontmatter, _ = _bytes_at(note_id, relative, path, schema, if_match)

            problems = schema.validate_frontmatter(frontmatter)
            if problems:
                raise ValidationFailed(problems)

            if request.preserve_body:
                data = current_data
                _, new_body = fm.parse(current_data.decode("utf-8"))
            else:
                data, new_body = _renamed_bytes(current_data, new_title)
            mirror_title = _title_of(new_body, path) if request.preserve_body else new_title

            new_stem = _stem_for(new_title, frontmatter, schema, settings)
            new_path = path.with_name(f"{new_stem}{NOTE_SUFFIX}")
            new_relative = new_path.relative_to(self.notes_root).as_posix()
            if new_path != path and new_stem.casefold() in {
                name.casefold() for name in existing_stems(path.parent) if name != path.stem
            }:
                raise PathCollision(new_relative)

            now = datetime.now(tz=UTC)
            digest = content_hash(data)
            try:
                async with transaction(self.session_factory) as session:
                    if new_path != path:
                        occupied = (
                            (
                                await session.execute(
                                    sa.select(Note.id)
                                    .where(
                                        Note.path == new_relative,
                                        Note.state.not_in(("missing", "deleted")),
                                    )
                                    .limit(1)
                                )
                            )
                            .scalars()
                            .first()
                        )
                        if occupied is not None:
                            raise PathCollision(new_relative)
                    await session.execute(
                        sa.update(Note)
                        .where(Note.id == note_id)
                        .values(
                            title=mirror_title,
                            path=new_relative,
                            content_hash=digest,
                            size_bytes=len(data),
                            mtime=now,
                            frontmatter=_jsonable(frontmatter),
                            **_mirror_columns(frontmatter, schema),
                            state="ok",
                            state_reason=None,
                            updated_at=now,
                        )
                    )
                    await index_note(
                        session,
                        note_id=note_id,
                        path=new_relative,
                        title=mirror_title,
                        body=new_body,
                        version=digest,
                        now=now,
                        notes_folder=settings.notes.notes_folder,
                    )
                    session.add(
                        OutboxEvent(
                            event_type="note.renamed",
                            note_id=note_id,
                            payload={"from_path": relative, "path": new_relative},
                            created_at=now,
                        )
                    )
                    await session.flush()
                    if new_path == path:
                        _replace_if_unchanged(note_id, relative, path, data, schema, if_match)
                    else:
                        _rename_file(
                            note_id, relative, path, new_path, new_relative, data, schema, if_match
                        )
                        self._identity_index.moved(note_id, new_path)
            except BaseException as exc:
                typed = _metadata_failure(exc)
                if typed is None:
                    raise
                raise typed from exc

        return await self._with_filing_state(
            _document(note_id, new_relative, mirror_title, frontmatter, new_body, data, now, schema)
        )

    async def list_folders(self) -> FolderTree:
        """Walk the notes filesystem and build a tree with note counts.

        The tree is built from what is on disk, not from the mirror, so it
        reflects the actual file structure including folders the mirror has not
        yet seen.
        """
        notes_root = self.notes_root
        seen_folders: dict[str, dict] = {}

        if not notes_root.is_dir():
            return FolderTree(children=[])

        for note_path in notes_root.rglob(f"*{NOTE_SUFFIX}"):
            if not note_path.is_file():
                continue
            relative = note_path.relative_to(notes_root)
            parts = relative.parts[:-1]  # folder parts only
            parent_key = ""
            folder_note_count: dict[str, int] = {}

            # Count this note under every ancestor folder
            for i in range(len(parts)):
                folder_key = "/".join(parts[: i + 1]) if i > 0 else parts[0]
                folder_note_count[folder_key] = folder_note_count.get(folder_key, 0) + 1

            # Build folder tree structure
            for i in range(len(parts)):
                folder_key = "/".join(parts[: i + 1]) if i > 0 else parts[0]
                parent_key_for_child = "/".join(parts[: i + 1]) if i > 0 else ""

                if folder_key not in seen_folders:
                    seen_folders[folder_key] = {
                        "name": parts[i],
                        "path": folder_key,
                        "children": {},
                    }

                # Add to parent's children
                if parent_key_for_child:
                    if parent_key_for_child in seen_folders:
                        seen_folders[parent_key_for_child]["children"][parts[i]] = folder_key
                else:
                    # Top-level folder
                    pass

            # Store count
            if parts:
                for folder_key in folder_note_count:
                    if folder_key in seen_folders:
                        seen_folders[folder_key]["note_count"] = (
                            seen_folders[folder_key].get("note_count", 0)
                            + folder_note_count[folder_key]
                        )

        # Build the tree from seen_folders
        top_level: dict[str, str] = {}
        for key, _info in seen_folders.items():
            parts = tuple(key.split("/"))
            if len(parts) == 1:
                top_level[parts[0]] = key
            elif len(parts) > 1:
                parent_key = "/".join(parts[:-1])
                if parent_key in seen_folders:
                    parent_children = seen_folders[parent_key].setdefault("_children", {})
                    parent_children[parts[-1]] = key

        # Recursively build FolderItem tree
        def build_tree(key: str) -> FolderItem:
            info = seen_folders.get(key, {})
            name = info.get("name", key.split("/")[-1])
            path = info.get("path", key)
            count = info.get("note_count", 0)
            children_keys = info.get("_children", {})
            children = [build_tree(child_key) for child_key in sorted(children_keys.values())]
            return FolderItem(name=name, path=path, note_count=count, children=children)

        top_keys = sorted(top_level.values())
        children = [build_tree(k) for k in top_keys]

        return FolderTree(children=children)

    @writer
    async def rebuild_metadata(self) -> RebuildMetadataResult:
        """Re-read every note file and rebuild its mirrored metadata."""
        from coppermind_store import indexer
        from coppermind_store.artifacts import recover_artifacts
        from coppermind_store.filing import file_reviewed
        from coppermind_store.reconciler import reconcile_once
        from coppermind_store.recovery import recover_sources

        counts = await recover_sources(self)
        counts.update(await recover_artifacts(self))
        counts.update(await reconcile_once(self, full=True))
        counts.update(await file_reviewed(self))
        await indexer.rebuild(self)
        return RebuildMetadataResult(counts=counts)

    async def search(self, query: SearchQuery) -> Page[SearchHit]:
        """Full-text search over note bodies, titles and source projections."""
        from coppermind_store.search import search

        return await search(self, query)

    async def get_search_index_status(self) -> SearchIndexStatus:
        from coppermind_store.indexer import status

        return await status(self)

    async def rebuild_search_index(self) -> RebuildSearchIndexResult:
        """Start rebuilding the index from the notes filesystem, if not running."""
        from coppermind_store.indexer import start_rebuild

        return await start_rebuild(self)

    def search_rebuild_running(self) -> bool:
        return self._search_rebuild is not None and not self._search_rebuild.done()

    def start_search_rebuild(self) -> bool:
        """Run the rebuild in the background. False when one is already running."""
        if self.search_rebuild_running():
            return False
        self._search_rebuild = asyncio.create_task(
            self._run_search_rebuild(), name="coppermind-search-rebuild"
        )
        return True

    async def _run_search_rebuild(self) -> None:
        from coppermind_store import indexer

        try:
            counts = await indexer.rebuild(self)
        except Exception as exc:  # noqa: BLE001 - a background job reports, never crashes
            indexer.log.warning("search index rebuild failed", error_type=type(exc).__name__)
            await indexer.record_rebuild_failure(self, type(exc).__name__)
        else:
            indexer.log.info("search index rebuilt", **counts)

    @writer
    async def adopt_note(
        self,
        relative: str,
        expected_hash: str,
        *,
        schema: FrontmatterSchema,
        settings: ProductSettings,
    ) -> AdoptionResult:
        """Give a settled device-created file an identity and mirror it.

        The scan supplies the hash it observed after the quiet period. The
        file is read again here and checked once more immediately before an
        atomic replacement, so a device write wins the race without losing
        bytes. Nothing serialises against this: the reconciler is the only
        caller and it adopts one file at a time, which is also why it reads the
        schema and the settings once for a whole pass and hands them in: every
        refusal returns before the first await, so re-reading two state files
        per candidate would hold the event loop for a whole sweep.

        The identity is always written because the notes filesystem is its
        durable home. A tag list carrying an alias or a repeat has only its own
        lines rewritten to the canonical tags; tags are never a reason to refuse
        adoption, closed or not, because the person already wrote them.

        Other keys receive defaults only when the schema requires them of this
        note and it does not already carry a value; an optional
        key a person did not write stays unwritten, and existing values and
        body content are never replaced.

        A file already carrying an identity this store knows is not adopted:
        the reconciler never proposes one, because nothing observable tells a
        copy apart from a move whose delete has not arrived yet. Neither is a
        file already carrying source associations, because ingest wrote that
        and generated output is not a note a person made.
        """
        try:
            path = resolve(self.notes_root, relative)
        except ValueError:
            return "invalid", "path is not inside the notes filesystem"
        try:
            current, _ = _read(relative, path)
        except NotFound:
            return "changed", "the file is no longer there"
        if content_hash(current) != expected_hash:
            return "changed", "the file changed after it was read"
        try:
            text = current.decode("utf-8")
            frontmatter, body = fm.parse(text)
        except UnicodeDecodeError:
            return "invalid", "the file is not valid UTF-8"
        except fm.FrontmatterError as exc:
            return "invalid", exc.category

        sources = frontmatter.get(schema.role("sources_key"))
        if isinstance(sources, list) and sources:
            return "invalid", "the store generated this file, so it is not a note to adopt"

        carried_id = frontmatter.get(schema.role("id_key"))
        if carried_id is None:
            note_id = new_id()
        elif is_valid_id(carried_id):
            note_id = carried_id
        else:
            return "invalid", "the id it carries is not a valid identifier"

        changes = _adoption_changes(frontmatter, schema, settings, note_id)
        try:
            adopted_text = _with_canonical_tags(fm.fill_missing(text, changes), schema)
            adopted_frontmatter, adopted_body = fm.parse(adopted_text)
        except fm.FrontmatterError as exc:
            return "invalid", exc.category
        # Everything this file is judged on is decided here, before a
        # connection is asked for, so a note the schema refuses costs a read
        # and a parse however many times the scan rediscovers it.
        invalid = schema.invalid_keys(adopted_frontmatter)
        if invalid:
            return "invalid", f"keys the schema refused: {', '.join(invalid)}"
        mirrored = _jsonable(adopted_frontmatter)
        if _unstorable(mirrored):
            return "invalid", "a value it carries is one the mirror cannot store"
        data = adopted_text.encode("utf-8")
        now = datetime.now(tz=UTC)

        try:
            async with transaction(self.session_factory) as session:
                await session.execute(sa.text("SELECT 1"))
                if await session.scalar(sa.select(Note.id).where(Note.id == note_id)):
                    return "collision", "the id it carries is already a note"
                occupied = await session.scalar(
                    sa.select(Note.id)
                    .where(Note.path == relative, Note.state.not_in(("missing", "deleted")))
                    .limit(1)
                )
                if occupied is not None:
                    return "collision", "another note already claims this path"

                if data != current:
                    try:
                        staged = stage_bytes(path, data)
                    except OSError as exc:
                        raise NotesFilesystemUnavailable(str(exc)) from exc
                    try:
                        latest, _ = _read(relative, path)
                        if content_hash(latest) != expected_hash:
                            staged.unlink(missing_ok=True)
                            return "changed", "a device wrote to the file first"
                        commit_staged(staged, path)
                    except NotFound:
                        staged.unlink(missing_ok=True)
                        return "changed", "the file is no longer there"
                    except OSError as exc:
                        staged.unlink(missing_ok=True)
                        raise NotesFilesystemUnavailable(str(exc)) from exc
                    except BaseException:
                        staged.unlink(missing_ok=True)
                        raise
                else:
                    try:
                        latest, _ = _read(relative, path)
                    except NotFound:
                        return "changed", "the file is no longer there"
                    if content_hash(latest) != expected_hash:
                        return "changed", "a device wrote to the file first"

                try:
                    file_stat = path.stat()
                except OSError as exc:
                    raise NotesFilesystemUnavailable(str(exc)) from exc
                session.add(
                    Note(
                        id=note_id,
                        path=relative,
                        title=_title_of(adopted_body, path),
                        content_hash=content_hash(data),
                        size_bytes=len(data),
                        mtime=datetime.fromtimestamp(file_stat.st_mtime, tz=UTC),
                        frontmatter=mirrored,
                        **_mirror_columns(adopted_frontmatter, schema),
                        state="ok",
                        first_seen_at=now,
                        updated_at=now,
                    )
                )
                await index_note(
                    session,
                    note_id=note_id,
                    path=relative,
                    title=_title_of(adopted_body, path),
                    body=adopted_body,
                    version=content_hash(data),
                    now=now,
                    notes_folder=self.control.settings().notes.notes_folder,
                )
        except BaseException as exc:
            typed = _metadata_failure(exc)
            if typed is None:
                raise
            raise typed from exc
        return "adopted", "identity written into the file"

    def _lock_for(self, note_id: NoteId) -> asyncio.Lock:
        """The lock serialising reads and writes to one note identity."""
        lock = self._locks.get(note_id)
        if lock is None:
            lock = self._locks[note_id] = asyncio.Lock()
        return lock

    def _source_lock_for(self, claim_name: str) -> asyncio.Lock:
        """Serialise replays and revisions for one durable source claim."""
        lock = self._source_locks.get(claim_name)
        if lock is None:
            lock = self._source_locks[claim_name] = asyncio.Lock()
        return lock

    async def _with_filing_state(self, document: NoteDocument) -> NoteDocument:
        from coppermind_store.filing import filing_state

        try:
            async with self.session_factory() as session:
                filed = await session.scalar(sa.select(Note.filed).where(Note.id == document.id))
        except (SQLAlchemyError, OSError) as exc:
            raise MetadataUnavailable(str(exc)) from exc
        return document.model_copy(update={"filed": bool(filed), **filing_state(self, document.id)})

    async def _followed(self, note_id: NoteId, inline: bool) -> list[Path]:
        """Where a note identity sits now, from the store-wide index.

        Callers holding a note lock pass `inline=False`: a stale index raises
        `_IdentityStale` so they can refresh it after releasing the lock.
        """
        index = self._identity_index
        matches = index.lookup(note_id)
        if matches is None:
            if not inline:
                raise _IdentityStale
            await index.refresh()
            matches = index.lookup(note_id) or []
        return matches

    async def _locate(self, note_id: NoteId, inline: bool = True) -> tuple[str, Path]:
        """Return the note's relative path and its absolute path.

        The path lives in PostgreSQL, so this is one of the reads that a
        database outage turns into a 503. The captain's rule applies: the
        notes filesystem stays fully usable and Obsidian keeps syncing, but
        the API does not invent state to keep answering.
        """
        try:
            async with self.session_factory() as session:
                located = (
                    await session.execute(
                        sa.select(Note.path, Note.state).where(Note.id == note_id)
                    )
                ).one_or_none()
        except (SQLAlchemyError, OSError) as exc:
            raise MetadataUnavailable(str(exc)) from exc
        if located is None or located.state in {"missing", "deleted"}:
            raise NotFound(note_id)
        relative = located.path
        try:
            path = resolve(self.notes_root, relative)
        except ValueError as exc:
            # The mirrored path leaves the notes filesystem, so the store
            # refuses to follow it. Nothing servable is there.
            raise NotFound(note_id) from exc
        if not is_note_file(path):
            # A move can become visible before its mirror transaction commits.
            # Follow the identity, never a filename alone or a different note.
            matches = await self._followed(note_id, inline)
            if len(matches) != 1:
                raise NotFound(note_id)
            path = matches[0]
            previous = relative
            relative = path.relative_to(self.notes_root).as_posix()
            await self._repair_location(note_id, previous, relative)
        return relative, path

    async def _repair_location(self, note_id: str, previous: str, observed: str) -> None:
        from coppermind_store.notices import clear_path_notice, record_path_notice

        try:
            async with transaction(self.session_factory) as session:
                # A settling file can have replaced another still-live row's
                # path. Follow its identity for this read, but leave ownership
                # decisions to reconciliation until that ambiguity settles.
                other = Note.__table__.alias("other_note")
                occupied = sa.exists().where(
                    other.c.id != note_id,
                    other.c.path == observed,
                    other.c.state.not_in(("missing", "deleted")),
                )
                updated = await session.execute(
                    sa.update(Note)
                    .where(Note.id == note_id, Note.path == previous, Note.state == "ok", ~occupied)
                    .values(path=observed, mtime=None, updated_at=datetime.now(UTC))
                    .returning(Note.id)
                )
                adopted = updated.scalar_one_or_none() is not None
                if adopted:
                    await session.execute(
                        sa.update(SearchDocument)
                        .where(SearchDocument.kind == "note", SearchDocument.ref_id == note_id)
                        .values(path=observed)
                    )
                current = await session.scalar(sa.select(Note.path).where(Note.id == note_id))
        except (SQLAlchemyError, OSError) as exc:
            raise MetadataUnavailable(str(exc)) from exc
        if adopted or current == observed:
            clear_path_notice(self, note_id)
        elif current == previous:
            record_path_notice(self, note_id, previous, observed)

    def _require_notes_root(self) -> None:
        try:
            with os.scandir(self.notes_root):
                pass
        except OSError as exc:
            raise NotesFilesystemUnavailable("notes storage unavailable, retry shortly") from exc

    def _scan_identities(self) -> dict[str, list[Path]]:
        from coppermind_store.reconciler import _IGNORED_DIRECTORIES, _path_exclusion, _walk

        # Every file the walk reached, keyed by the identity it carries. The root
        # itself is opened first, so an empty map means the tree holds nothing and a
        # failed open means the volume is away; both answer from the same OSError.
        with os.scandir(self.notes_root):
            pass
        found: dict[str, list[Path]] = {}
        key = self.control.schema().role("id_key")
        settings = self.control.settings()
        for candidate in _walk(self.notes_root):
            relative = candidate.relative_to(self.notes_root)
            if candidate.suffix != ".md" or any(
                part in _IGNORED_DIRECTORIES for part in relative.parts
            ):
                continue
            if _path_exclusion(
                relative, settings.reconcile.excluded_folders, settings.reconcile.excluded_patterns
            ):
                continue
            try:
                safe = resolve(self.notes_root, relative.as_posix())
                if not is_note_file(safe):
                    continue
                identity = self.note_history.identity(safe.read_bytes(), key)
            except (OSError, ValueError):
                continue
            if identity is not None:
                found.setdefault(identity, []).append(safe)
        return found


class _IdentityStale(Exception):
    """The identity index must be refreshed, outside any note lock, before this read."""


class _IdentityIndex:
    """One store-wide map from note identity to file path, shared by every reader.

    It is rebuilt only when a read of a stale row finds it missing, expired
    (older than one reconciler interval) or unable to answer after the few
    seconds a fresh scan stays authoritative. Within that window an identity
    the scan did not find is not found, so a burst of reads of rows whose
    files are gone costs one tree walk, not one each.
    """

    MISS_WINDOW_S = 5.0

    def __init__(self, store: LocalStore) -> None:
        self._store = store
        self._found: dict[str, list[Path]] | None = None
        self._built = 0.0
        self._lock = asyncio.Lock()

    def lookup(self, note_id: str) -> list[Path] | None:
        """The paths carrying this identity, or None when the index must be rebuilt."""
        if self._found is None:
            return None
        age = time.monotonic() - self._built
        interval = self._store.control.settings().reconcile.scan_interval_s
        live = [path for path in self._found.get(note_id, []) if is_note_file(path)]
        if live and age <= interval:
            return live
        if age > self.MISS_WINDOW_S:
            return None
        return live

    def moved(self, note_id: str, path: Path) -> None:
        if self._found is not None:
            self._found[note_id] = [path]

    async def refresh(self) -> None:
        """Rebuild the index, or carry the notes volume's own error as the cause."""
        requested = time.monotonic()
        async with self._lock:
            if self._built >= requested:
                return
            started = time.monotonic()
            try:
                found = await asyncio.to_thread(self._store._scan_identities)
            except OSError as exc:
                # The tree walk reports the unreadable subfolder or missing root
                # by name, so it is carried as the cause of the typed failure.
                raise NotesFilesystemUnavailable(str(exc)) from exc
            self._found, self._built = found, started


def _parse(
    note_id: NoteId,
    relative: str,
    data: bytes,
    schema: FrontmatterSchema,
) -> tuple[dict[str, Any], str]:
    """Parse a located note's bytes and check they are that note's."""
    try:
        frontmatter, body = fm.parse(data.decode("utf-8"))
    except (fm.FrontmatterError, UnicodeDecodeError) as exc:
        if isinstance(exc, fm.FrontmatterError):
            # A person broke this file on a device. Before announcing the file
            # as unparseable, try to learn the identity from the raw block so
            # a rename onto a stale path can answer NotFound rather than
            # NoteUnparseable. The id key name comes from the schema, not the
            # file, so it cannot carry content.  Decode only here because a
            # FrontmatterError implies the bytes were valid UTF-8 (the decode
            # in the try block succeeded enough to reach the YAML parser).
            text = data.decode("utf-8")
            recovered_id = _recover_id_from_raw(text, schema.role("id_key"))
            if recovered_id is not None and recovered_id != note_id:
                raise NotFound(note_id) from None
        # A person broke this file on a device. That is not a fault of the
        # store, and the answer says so rather than blaming Coppermind.
        # The parser's reason quotes the offending lines, so it is the
        # person's own note content: it reaches the internal surface, which
        # the store alone answers, and never the log or the public
        # envelope. Logs are collected and shipped, so the log gets only what
        # the type of the failure and its position say.
        # Scheduled reconciliation records the state without logging note
        # content. This warning belongs to the read that asked for the note.
        log.warning(
            "note frontmatter could not be parsed",
            note_id=note_id,
            path=relative,
            **_parse_failure_fields(exc),
        )
        raise NoteUnparseable(note_id, str(exc)) from exc
    carried_id = frontmatter.get(schema.role("id_key"))
    if carried_id is not None and str(carried_id) != note_id:
        # The row outlived the file it named, because a note was deleted on
        # a device and another was renamed into its place. Serving this file
        # would answer a different note under the requested identifier, so
        # the honest answer is a miss until reconciliation clears the row.
        raise NotFound(note_id)
    return frontmatter, body


def _mirrored_summary(row: Note, state: NoteState) -> NoteSummary:
    """The latest reconciled metadata and state for a known identity."""
    return NoteSummary(
        id=row.id,
        path=row.path,
        state=state,
        state_reason=row.state_reason,
        title=row.title or Path(row.path).stem,
        date=row.date,
        type=row.type,
        context=row.context,
        account=list(row.account),
        reviewed=row.reviewed,
        filed=row.filed,
        tags=list(row.tags),
        content_hash=row.content_hash,
        updated_at=row.mtime or row.updated_at,
    )


def _matches_query(summary: NoteSummary, query: NoteQuery) -> bool:
    return all(
        (
            query.folder is None or summary.path.startswith(f"{query.folder}/"),
            query.reviewed is None or summary.reviewed is query.reviewed,
            query.type is None or summary.type == query.type,
            query.context is None or summary.context == query.context,
            query.account is None or query.account in summary.account,
            query.from_date is None or summary.date is not None and summary.date >= query.from_date,
            query.to_date is None or summary.date is not None and summary.date <= query.to_date,
            query.tag is None or query.tag in summary.tags,
            query.state is None or summary.state == query.state,
        )
    )


def _encode_note_cursor(note_id: str, context: str | None = None) -> str:
    payload = json.dumps(
        {"v": 3 if context else 2, "id": note_id, "context": context}, separators=(",", ":")
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_note_cursor(cursor: str, context: str | None = None) -> str:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        if (
            not isinstance(payload, dict)
            or payload.get("v") not in (2, 3)
            or not isinstance(payload.get("id"), str)
        ):
            raise ValueError
        if payload.get("context") != context:
            raise FilterCursorConflict()
        return payload["id"]
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise ValidationFailed(["cursor: invalid or expired"]) from exc


def _replace_if_unchanged(
    note_id: NoteId,
    relative: str,
    path: Path,
    data: bytes,
    schema: FrontmatterSchema,
    if_match: ETag,
) -> None:
    """Rename `data` over the note's file only if it still hashes to `if_match`.

    The new bytes are staged and made durable first, so what stands between
    the last compare and the rename is the compare itself. The per-note lock
    cannot exclude Obsidian Sync writing the same file, and there is no
    conditional rename to ask the filesystem for, so this is the narrowest
    window a replace can have. A refused or failed replace leaves no staged
    file behind.
    """
    try:
        staged = stage_bytes(path, data)
    except OSError as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc
    try:
        _bytes_at(note_id, relative, path, schema, if_match)
        commit_staged(staged, path)
    except OSError as exc:
        staged.unlink(missing_ok=True)
        raise NotesFilesystemUnavailable(str(exc)) from exc
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _bytes_at(
    note_id: NoteId, relative: str, path: Path, schema: FrontmatterSchema, if_match: ETag | None
) -> tuple[bytes, dict[str, Any], float]:
    """The bytes, frontmatter and mtime of a note at `if_match`.

    The identity check comes before the compare, so a row whose file is now
    another note is a miss whatever ETag was sent, not a conflict naming the
    other note's hash. One parse serves both the identity check and the
    caller, so nothing but that check and the compare sits inside the window
    `_replace_if_unchanged` holds open before the rename.
    """
    current, mtime = _read(note_id, path)
    frontmatter, _ = _parse(note_id, relative, current, schema)
    current_hash = content_hash(current)
    if if_match is not None and current_hash != if_match:
        raise VersionConflict(current_hash)
    return current, frontmatter, mtime


def _read(note_id: NoteId, path: Path) -> tuple[bytes, float]:
    """The bytes and mtime of a located note, with typed filesystem failures.

    An absent path is the note being `missing`. Any other operating system error
    is the notes volume, and is raised as the typed notes outage with that error
    as its cause.
    """
    try:
        return path.read_bytes(), path.stat().st_mtime
    except FileNotFoundError as exc:
        raise NotFound(note_id) from exc
    except OSError as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc


def _metadata_failure(exc: BaseException) -> StoreError | None:
    """The typed error a database failure maps to, or None if it is not one.

    A connection refused by asyncpg arrives as a bare OSError. The filesystem
    writes raise their own typed errors where they happen, so what reaches
    this level is the database and only the database.
    """
    if isinstance(exc, DBAPIError) and str(getattr(exc.orig, "sqlstate", "")).startswith("22"):
        return ValidationFailed(["request contains a value the metadata store cannot accept"])
    if isinstance(exc, SQLAlchemyError | OSError):
        return MetadataUnavailable(str(exc))
    return None


def _parse_failure_fields(exc: fm.FrontmatterError | UnicodeDecodeError) -> dict[str, Any]:
    """Describe a parse failure without repeating any of the note back.

    The category names the kind of failure from the exception type, and the
    position says where to look. Neither can carry a key, a value or a line of
    the file, which the parser's own message does.
    """
    if isinstance(exc, fm.FrontmatterError):
        return {
            "category": exc.category,
            "frontmatter_line": exc.line,
            "frontmatter_column": exc.column,
        }
    return {"category": "undecodable_bytes", "byte_offset": exc.start}


def _recover_id_from_raw(
    text: str,
    id_key: str,
) -> str | None:
    """Return the id value on a line matching ``<id_key>: <value>`` inside the frontmatter block.

    The frontmatter block is split from the body using ``fm.split``, then the
    block lines are scanned for a literal key-value pair.  The value is
    stripped of inline comments and quoting so it matches what the store
    writes as the identity.  ``None`` means the key could not be found in the
    raw block, so nothing can be compared against.
    """
    try:
        block, _ = fm.split(text)
    except fm.FrontmatterError:
        # The block is never closed (or another split error); fall back to
        # the caller's normal NoteUnparseable path rather than leaking a 500.
        return None
    if not block.strip():
        return None
    pattern = re.compile(rf"^{re.escape(id_key)}\s*:\s*(.+)$")
    for raw_line in block.splitlines():
        line = raw_line.split("#", 1)[0] if "#" in raw_line else raw_line
        m = pattern.match(line)
        if m:
            value = m.group(1).strip()
            # Strip surrounding quotes the YAML spec allows.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            return value
    return None


def _default_note_time(
    frontmatter: dict[str, Any],
    schema: FrontmatterSchema,
    settings: ProductSettings,
    captured_at: datetime | None = None,
) -> str:
    instant = (
        captured_at.astimezone(ZoneInfo(settings.general.timezone))
        if captured_at is not None
        else datetime.now(tz=ZoneInfo(settings.general.timezone))
    )
    explicit_date = _as_date(frontmatter.get(schema.role("date_key")))
    if explicit_date is not None:
        return format_note_time(
            datetime.combine(explicit_date, instant.time().replace(microsecond=0))
        )
    return format_note_time(instant)


def _build_frontmatter(
    request: CreateNote,
    schema: FrontmatterSchema,
    settings: ProductSettings,
    note_id: str,
    *,
    captured_at: datetime | None = None,
    from_ingest: bool = False,
) -> dict[str, Any]:
    """Merge the caller's frontmatter over the schema defaults, in key order."""
    values: dict[str, Any] = schema.defaults()
    date_key = schema.role("date_key")
    supplied = request.frontmatter.get(date_key)
    given = parse_note_time(supplied)
    legacy_time = request.frontmatter.get("time")
    legacy = parse_note_time(legacy_time)
    needs_default = (not supplied and not values.get(date_key) and not legacy_time) or (
        from_ingest
        and request.frontmatter.get("time_unknown") is not True
        and not legacy_time
        and (not supplied or (given.valid and given.time_value is None))
    )
    # The start time lives in `date`, so a note without one gets the capture
    # moment, on its own day when it names one.
    default_date = (
        _default_note_time(request.frontmatter, schema, settings, captured_at)
        if needs_default
        else None
    )
    values.update(request.frontmatter)
    if default_date is not None:
        values[date_key] = default_date
    elif from_ingest and given.valid and given.time_value is not None:
        values[date_key] = normalize_note_time(supplied, fallback_date=_today(settings))
    elif from_ingest and not supplied and legacy.valid and legacy.time_value is not None:
        capture_day = parse_note_time(
            _default_note_time(request.frontmatter, schema, settings, captured_at)
        ).date_value
        values[date_key] = normalize_note_time(legacy_time, fallback_date=capture_day)
    values[schema.role("id_key")] = note_id
    values.setdefault(schema.role("schema_version_key"), 1)
    return _ordered(values, schema)


def _adoption_changes(
    frontmatter: dict[str, Any],
    schema: FrontmatterSchema,
    settings: ProductSettings,
    note_id: str,
) -> dict[str, Any]:
    """The identity and defaults needed to make a device-created note valid.

    Identity is durable only when it is in the file, regardless of whether an
    edited schema calls that key optional. Every other key the schema does not
    require is left out even when it ships a default, because adoption writes
    into a file a person owns and every key it adds syncs back to their devices.
    A required key the file carries with no value counts as one to fill: the
    schema reads it as absent, so leaving it would refuse the note for a
    property a person added and left empty. Requirements are recalculated after
    each applicable default because one default can activate another key's
    conditional requirement.
    """
    available = schema.defaults()
    id_key = schema.role("id_key")
    date_key = schema.role("date_key")
    available[id_key] = note_id
    available.setdefault(schema.role("schema_version_key"), 1)
    available.setdefault(date_key, _today(settings))
    changes: dict[str, Any] = {id_key: note_id} if frontmatter.get(id_key) is None else {}
    if frontmatter.get(date_key) is None and note_date(frontmatter, schema) is None:
        changes[date_key] = (
            _today(settings)
            if parse_note_time(frontmatter.get("time")).present
            else _default_note_time(frontmatter, schema, settings)
        )
    effective = {**frontmatter, **changes}
    while True:
        added = False
        required = schema.required_keys(effective)
        for definition in schema.keys:
            name = definition.name
            if name not in required or effective.get(name) is not None or name not in available:
                continue
            changes[name] = available[name]
            effective[name] = available[name]
            added = True
        if not added:
            return changes


def _with_canonical_tags(text: str, schema: FrontmatterSchema) -> str:
    """The note text with its tag list normalized in place, or unchanged.

    Only the tag lines change, so adoption stays append only for every other
    byte, line endings included. When the list cannot be rewritten without
    touching anything else (a comment inside it, say) it is left as the person
    wrote it.
    """
    tags_key = schema.role("tags_key")
    tags = fm.parse(text)[0].get(tags_key)
    if not isinstance(tags, list):
        return text
    normalized = schema.normalize_tags(list(tags))
    if normalized == list(tags):
        return text
    return fm.replace_value_in_place(text, tags_key, normalized) or text


def _replacement_frontmatter(
    request: ReplaceNote, schema: FrontmatterSchema, note_id: str
) -> dict[str, Any]:
    """The caller's frontmatter as sent, carrying the identifier the store keeps.

    No defaults are filled in: a replace lands what was sent, and the schema
    says whether that is a complete note. The document a read returns already
    carries every key, so a read, edit, write round trip needs nothing added.
    """
    values = dict(request.frontmatter)
    id_key = schema.role("id_key")
    carried_id = values.get(id_key)
    if carried_id is not None and str(carried_id) != note_id:
        raise ValidationFailed([f"{id_key}: the identifier of a note cannot be changed"])
    values[id_key] = note_id
    return _ordered(values, schema)


def _keeping_types(frontmatter: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """The frontmatter to write, with each key sent back unchanged as the file holds it.

    A read hands out a date as text, so a key nobody edited comes back as a
    string. Writing the file's own value keeps it a date for Obsidian and
    keeps the untouched key out of what Obsidian Sync pushes to every device.
    """
    return {
        key: current[key]
        if key in current and json.dumps(_jsonable(value)) == json.dumps(_jsonable(current[key]))
        else value
        for key, value in frontmatter.items()
    }


def _with_kinds(values: dict[str, Any], schema: FrontmatterSchema) -> dict[str, Any]:
    """The caller's values with each known key written in the kind the schema gives it.

    A date key is written as a YAML date, not a quoted string, so the file
    reads the way a person would write it in Obsidian and no write path,
    whole document or single key, adds quotes to it. A tag list has each alias
    replaced by its canonical tag; create, ingest, replace and patch all come
    through here, which is what makes aliases hold on every API write.
    """
    kinds = {definition.name: definition.kind for definition in schema.keys}
    tags_key = schema.role("tags_key")
    return {
        key: _date_value(value)
        if kinds.get(key) == "date"
        else schema.normalize_tags(value)
        if key == tags_key and isinstance(value, list)
        else value
        for key, value in values.items()
    }


def _ordered(values: dict[str, Any], schema: FrontmatterSchema) -> dict[str, Any]:
    """Known keys in schema order with their kinds applied, then the rest as given."""
    values = _with_kinds(values, schema)
    ordered: dict[str, Any] = {}
    known_keys = {definition.name for definition in schema.keys}
    for definition in schema.keys:
        if definition.name in values and values[definition.name] is not None:
            ordered[definition.name] = values[definition.name]
    # Keys the schema does not know about are kept, after the known ones.
    for key, value in values.items():
        if key not in known_keys:
            ordered[key] = value
    return ordered


def _patch_problems(request: PatchFrontmatter, schema: FrontmatterSchema) -> list[str]:
    """Problems with the patch request itself, found before any file is read.

    A key named in both halves has no single meaning, neither the identifier a
    note is found by nor a key the schema requires is a patch's to remove, and
    a null is not a value to write: removing a key is what `unset` is for, so
    `set` has one meaning rather than two. Refusing here means the file is
    never opened, let alone written.
    """
    id_key = schema.role("id_key")
    sources_key = schema.role("sources_key")
    required = {definition.name for definition in schema.keys if definition.required}
    unset = set(request.unset)
    problems = [f"{key}: named in both set and unset" for key in request.set if key in unset]
    problems += [
        f"{key}: null is not a value to write; name the key in unset to remove it"
        for key, value in request.set.items()
        if value is None
    ]
    if sources_key in request.set or sources_key in unset:
        problems.append(
            f"{sources_key}: source associations are managed by ingest and cannot be patched"
        )
    for key in request.unset:
        if key == id_key:
            problems.append(f"{key}: the identifier of a note cannot be removed")
        elif key in required:
            problems.append(f"{key}: required, so it cannot be removed")
    return problems


def _check_folder_safe(folder: str, settings: ProductSettings) -> None:
    """Refuse a folder that would place notes inside _Sources or the artifacts folder.

    Compared casefolded, since a notes filesystem that is case-insensitive
    (or an Obsidian device that treats "_sources" and "_Sources" as the same
    entry) would otherwise let a differently-cased folder name through the
    check but collide with the real _Sources folder on disk. Nothing but the
    artifact files themselves ever appears under the artifacts folder.
    """
    parts = [p for p in folder.split("/") if p]
    for reserved in (settings.notes.sources_folder, settings.notes.artifacts_folder):
        reserved_folder = sanitize_folder(reserved).casefold()
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i]).casefold()
            if prefix == reserved_folder or prefix.startswith(f"{reserved_folder}/"):
                raise ValidationFailed([f"folder: cannot place a note inside {reserved}"])


def _mirror_columns(frontmatter: dict[str, Any], schema: FrontmatterSchema) -> dict[str, Any]:
    """The columns of the mirror row that project individual frontmatter keys."""
    tags = frontmatter.get(schema.role("tags_key"), [])
    sources = frontmatter.get(schema.role("sources_key"), [])
    artifacts = frontmatter.get(schema.optional_role("artifacts_key"), [])
    return {
        "source_ids": sorted({s for s in sources if isinstance(s, str)})
        if isinstance(sources, list)
        else [],
        "artifact_ids": sorted({a for a in artifacts if isinstance(a, str)})
        if isinstance(artifacts, list)
        else [],
        "schema_version": _as_int(frontmatter.get(schema.role("schema_version_key"), 1)),
        "type": _text(frontmatter.get(schema.role("type_key"))),
        "context": _text(frontmatter.get(schema.role("context_key"))),
        "account": [
            _storable(name) for name in account_list(frontmatter.get(schema.role("account_key")))
        ],
        "date": note_date(frontmatter, schema),
        "reviewed": frontmatter.get(schema.role("reviewed_key")) is True,
        **({"filed": False} if frontmatter.get(schema.role("reviewed_key")) is not True else {}),
        "tags": [_storable(str(tag)) for tag in tags] if isinstance(tags, list) else [],
    }


def _stem_for(
    title: str,
    frontmatter: dict[str, Any],
    schema: FrontmatterSchema,
    settings: ProductSettings,
) -> str:
    note_type = _text(frontmatter.get(schema.role("type_key")))
    dated = note_type in settings.notes.dated_types
    return note_stem(title, note_date=note_date(frontmatter, schema), dated=dated)


def _body_with_heading(title: str, body: str) -> str:
    heading = f"# {title}".rstrip()
    text = body.lstrip("\n")
    composed = f"{heading}\n\n{text}" if text else f"{heading}\n"
    return _terminated(composed)


def _terminated(body: str) -> str:
    """A body that ends with a newline, or is empty."""
    return body if not body or body.endswith("\n") else body + "\n"


def _title_of(body: str, path: Path) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return _storable(line[2:].strip())
    return _storable(path.stem)


def _document(
    note_id: NoteId,
    relative: str,
    title: str,
    frontmatter: dict[str, Any],
    body: str,
    data: bytes,
    updated_at: datetime,
    schema: FrontmatterSchema,
) -> NoteDocument:
    sources = frontmatter.get(schema.role("sources_key"), [])
    return NoteDocument(
        id=note_id,
        path=relative,
        title=title,
        frontmatter=_jsonable(frontmatter),
        body=body,
        content_hash=content_hash(data),
        size_bytes=len(data),
        updated_at=updated_at,
        sources=[str(source) for source in sources] if isinstance(sources, list) else [],
    )


def _today(settings: ProductSettings) -> date:
    """Today in the operator's timezone."""
    return datetime.now(tz=ZoneInfo(settings.general.timezone)).date()


def _text(value: Any) -> str | None:
    return _storable(value) if isinstance(value, str) else None


def _date_value(value: Any) -> Any:
    """A date as a day, or a date-and-time in canonical text, or the value as given."""
    parsed = parse_note_time(value)
    if not parsed.valid or parsed.date_value is None:
        return value
    if parsed.time_value is None:
        return parsed.date_value
    return format_note_time(datetime.combine(parsed.date_value, parsed.time_value))


def _as_date(value: Any) -> date | None:
    """The day of a date or a local date-and-time, or its string form."""
    parsed = parse_note_time(value)
    return parsed.date_value if parsed.valid else None


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


def _storable(text: str) -> str:
    """Drop what no PostgreSQL text or JSONB column can hold.

    A NUL is rejected outright, and it would be rejected at the insert, after
    adoption had already rewritten a file a person owns, leaving a note the
    store changed and then never delivered. The file keeps every byte; the
    mirror, which is only ever a mirror, does not.
    """
    return text.replace("\x00", "") if "\x00" in text else text


def _unstorable(value: Any) -> bool:
    """Whether a mirrored value is one PostgreSQL will refuse outright.

    A non-finite float is not JSON and a lone surrogate is not encodable, and
    either reaches the insert, which for adoption happens after a person's file
    has already been rewritten. Adoption asks this before it writes so such a
    file is refused like any other and keeps every byte. The public create path
    deliberately does not ask: a caller who sent the value is owed a validation
    error from the insert, with no file left behind.
    """
    if isinstance(value, dict):
        return any(_unstorable(key) or _unstorable(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_unstorable(item) for item in value)
    if isinstance(value, float):
        return not isfinite(value)
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return True
    return False


def _jsonable(value: Any) -> Any:
    """Convert a round tripped YAML mapping into plain JSON friendly types.

    A person's frontmatter may hold a tagged scalar, binary or a set, and a
    string may carry a NUL; none of those can be stored in the mirror's JSONB
    column, and one escaping into an insert would fail it after the file had
    already been written. The file stays the truth, so anything that is not a
    JSON value is mirrored as its text.

    A number is passed through as it stands, an infinity included. A value a
    caller sent that PostgreSQL will not take has to reach the insert and be
    refused there, because the public write contract answers that request with
    a validation error and no file, rather than storing something else in its
    place.
    """
    if isinstance(value, dict):
        return {_storable(str(k)): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, str):
        return _storable(value)
    if isinstance(value, bool) or value is None or isinstance(value, int | float):
        return value
    return _storable(str(value))
