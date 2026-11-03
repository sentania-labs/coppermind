"""Derived search text, published only after review and monthly filing.

Human edits update published notes in the mirror transaction. Catch-up and
rebuild apply the same publication gate. Source text additionally requires a
reviewed projection for the current archive revision. Originals remain outside
the search filesystem.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from coppermind import frontmatter as fm
from coppermind.db.models import (
    Note,
    NoteSource,
    SearchDocument,
    SearchIndexState,
    SearchUnreadable,
    Source,
)
from coppermind.db.session import transaction
from coppermind.logging import get_logger
from coppermind.naming import sanitize_folder
from coppermind.store_protocol import (
    MetadataUnavailable,
    RebuildSearchIndexResult,
    SearchIndexStatus,
    StoreError,
)
from coppermind_store.eligibility import current_source, eligible, reviewed_note
from coppermind_store.fs import content_hash, resolve

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore

log = get_logger("coppermind-store")

# How much of one body is indexed. PostgreSQL refuses a tsvector over 1 MB,
# and a refusal inside a note write would fail the write. Positions past
# 16,383 are not recorded anyway, so the tail of a very long transcript adds
# little; it is still searchable up to here and readable in full on disk.
MAX_INDEXED_CHARS = 250_000

# How many stale entries one catch-up step reads and indexes per kind. The
# rest wait for the next pass, which keeps one pass short after a large
# ingest or an upgrade.
CATCH_UP_BATCH = 200

# Rows per insert during a rebuild.
_REBUILD_BATCH = 200

_IGNORED_DIRECTORIES = {".git", ".obsidian", ".trash"}


def indexable(text: str) -> str:
    """Text PostgreSQL will store and index: no NUL, bounded length."""
    return text.replace("\x00", "")[:MAX_INDEXED_CHARS]


def source_version(revision: int) -> str:
    return f"r{revision}"


def _title_of(body: str, path: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return indexable(line[2:].strip())
    return indexable(Path(path).stem)


async def index_note(
    session: AsyncSession,
    *,
    note_id: str,
    path: str,
    title: str,
    body: str,
    version: str,
    now: datetime,
) -> None:
    """Write one note's entry inside the caller's transaction."""
    await _upsert(session, "note", note_id, path, title, body, version, now)


async def index_source(
    session: AsyncSession,
    *,
    source_id: str,
    path: str,
    title: str,
    body: str,
    revision: int,
    now: datetime,
) -> None:
    """Write one source projection's entry inside the caller's transaction."""
    await _upsert(session, "source", source_id, path, title, body, source_version(revision), now)


async def follow_note_path(session: AsyncSession, note_id: str, path: str) -> None:
    """Keep an entry's path current when its file moved but could not be read."""
    await session.execute(
        sa.update(SearchDocument)
        .where(
            SearchDocument.kind == "note",
            SearchDocument.ref_id == note_id,
            SearchDocument.path != path,
        )
        .values(path=path)
    )


async def drop_note(session: AsyncSession, note_id: str, now: datetime) -> None:
    """Remove a note's entry: its file is gone, so nothing should find it."""
    result = await session.execute(
        sa.delete(SearchDocument)
        .where(SearchDocument.kind == "note", SearchDocument.ref_id == note_id)
        .returning(SearchDocument.ref_id)
    )
    if result.first() is not None:
        await _changed(session, now)


async def _upsert(
    session: AsyncSession,
    kind: str,
    ref_id: str,
    path: str,
    title: str,
    body: str,
    version: str,
    now: datetime,
) -> None:
    values = {
        "kind": kind,
        "ref_id": ref_id,
        "path": path,
        "title": indexable(title),
        "body": indexable(body),
        "version": version,
        "indexed_at": now,
    }
    await session.execute(
        sa.delete(SearchUnreadable).where(
            SearchUnreadable.kind == kind, SearchUnreadable.ref_id == ref_id
        )
    )
    allowed = eligible(sa.literal(kind), sa.literal(ref_id))
    await session.execute(
        sa.delete(SearchDocument).where(
            SearchDocument.kind == kind, SearchDocument.ref_id == ref_id, ~allowed
        )
    )
    statement = insert(SearchDocument).from_select(
        list(values), sa.select(*(sa.literal(value) for value in values.values())).where(allowed)
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[SearchDocument.kind, SearchDocument.ref_id],
            set_={key: statement.excluded[key] for key in values if key not in {"kind", "ref_id"}},
        )
    )


async def _changed(session: AsyncSession, now: datetime) -> None:
    """Record a removal, which leaves no row whose time says it happened.

    Additions and edits are timed by their own rows, so a note write never
    contends for this one row with every other note write.
    """
    await session.execute(
        sa.update(SearchIndexState).where(SearchIndexState.id == 1).values(last_changed_at=now)
    )


async def status(store: LocalStore) -> SearchIndexStatus:
    """Counts and times for Admin, read from the index itself."""
    try:
        async with store.session_factory() as session:
            counts = dict(
                (
                    await session.execute(
                        sa.select(SearchDocument.kind, sa.func.count()).group_by(
                            SearchDocument.kind
                        )
                    )
                ).all()
            )
            known_unreadable = (
                await session.scalar(sa.select(sa.func.count()).select_from(SearchUnreadable)) or 0
            )
            newest = await session.scalar(sa.select(sa.func.max(SearchDocument.indexed_at)))
            state = await session.get(SearchIndexState, 1)
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
    times = [newest]
    if state is not None:
        times += [state.last_changed_at, state.rebuild_completed_at]
    known = [moment for moment in times if moment is not None]
    return SearchIndexStatus(
        known_unreadable=known_unreadable,
        notes=int(counts.get("note", 0)),
        sources=int(counts.get("source", 0)),
        last_updated_at=max(known) if known else None,
        rebuild_running=store.search_rebuild_running(),
        rebuild_started_at=state.rebuild_started_at if state else None,
        rebuild_completed_at=state.rebuild_completed_at if state else None,
        rebuild_error=state.rebuild_error if state else None,
    )


@dataclass(frozen=True)
class Entry:
    """One file's worth of index entry, read from the notes filesystem."""

    kind: str
    ref_id: str
    path: str
    title: str
    body: str
    version: str


def _read_note(
    root: Path,
    relative: str,
    note_id: str,
    expected_hash: str,
    id_key: str,
    reasons: list[str] | None = None,
) -> Entry | None:
    """The entry for a mirrored note, or None if the file is not that note now."""
    try:
        data = resolve(root, relative).read_bytes()
    except (OSError, ValueError) as exc:
        if reasons is not None:
            reasons.append(f"{type(exc).__name__}: {exc}")
        return None
    if content_hash(data) != expected_hash:
        if reasons is not None:
            reasons.append("file content differs from the mirrored content hash")
        # The file moved on since the mirror read it; the next pass mirrors the
        # new bytes and the entry follows from there.
        return None
    try:
        frontmatter, body = fm.parse(data.decode("utf-8"))
    except (UnicodeDecodeError, fm.FrontmatterError) as exc:
        if reasons is not None:
            reasons.append(f"{type(exc).__name__}: {exc}")
        return None
    if str(frontmatter.get(id_key, note_id)) != note_id:
        if reasons is not None:
            reasons.append("file note identity differs from the mirrored identity")
        return None
    return Entry("note", note_id, relative, _title_of(body, relative), body, expected_hash)


def _read_projection(
    root: Path, relative: str, source_id: str, revision: int, reasons: list[str] | None = None
) -> Entry | None:
    """The entry for a source's generated page, if the path still holds it."""
    try:
        text = resolve(root, relative).read_text(encoding="utf-8")
        frontmatter, body = fm.parse(text)
    except (OSError, ValueError, UnicodeDecodeError, fm.FrontmatterError) as exc:
        if reasons is not None:
            reasons.append(f"{type(exc).__name__}: {exc}")
        return None
    if (
        frontmatter.get("reviewed") is not True
        or frontmatter.get("managed") is not True
        or frontmatter.get("source_id") != source_id
        or frontmatter.get("source_revision") != revision
    ):
        if reasons is not None:
            reasons.append(
                "projection is unreviewed, unmanaged, or has a different source identity/revision"
            )
        return None
    return Entry(
        "source", source_id, relative, _title_of(body, relative), body, source_version(revision)
    )


def _projection_path(
    sources_root: Path, source_id: str, reasons: list[str] | None = None
) -> str | None:
    """The page a source's manifest records, the only record of where it lives."""
    from coppermind_store.sources import _manifest_document

    try:
        manifest = _manifest_document(sources_root / source_id / "manifest.json")
    except (OSError, StoreError) as exc:
        if reasons is not None:
            reasons.append(f"manifest {type(exc).__name__}: {exc}")
        return None
    recorded = manifest.get("projection_path")
    if isinstance(recorded, str) and recorded:
        return recorded
    if reasons is not None:
        reasons.append("manifest records no projection path")
    return None


async def index_projection(store: LocalStore, source_id: str, revision: int, path: str) -> bool:
    """Index the page an ingest just wrote. False when the path does not hold it."""
    entry = await asyncio.to_thread(_read_projection, store.notes_root, path, source_id, revision)
    if entry is None:
        return False
    async with transaction(store.session_factory) as session:
        await _write(session, entry, datetime.now(tz=UTC))
    return True


async def _write(session: AsyncSession, entry: Entry, now: datetime) -> None:
    await _upsert(
        session, entry.kind, entry.ref_id, entry.path, entry.title, entry.body, entry.version, now
    )


def _file_state(path: Path) -> str:
    """Detect changes without rereading known failed files."""
    try:
        stat = path.stat()
        return f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        return f"error:{exc.errno}"


def _relative_state(root: Path, path: str) -> str:
    if not path:
        return "no-path"
    try:
        return _file_state(resolve(root, path))
    except ValueError:
        return "invalid-path"
    except OSError as exc:
        return f"error:{exc.errno}"


async def catch_up(store: LocalStore) -> dict[str, int]:
    """Bring the index level with the mirror, a bounded batch at a time.

    Entries for notes that are gone are removed, entries whose note moved
    while its file could not be read take the new path, and up to
    `CATCH_UP_BATCH` notes and source projections the index describes at an
    older version, or not at all, are read from their files and indexed.
    """
    counts = {"indexed": 0, "removed": 0, "unreadable": 0, "skipped_known_unreadable": 0}
    id_key = store.control.schema().role("id_key")
    now = datetime.now(tz=UTC)
    live = sa.select(Note.id).where(Note.id == SearchDocument.ref_id, reviewed_note())
    try:
        async with transaction(store.session_factory) as session:
            removed = await session.execute(
                sa.delete(SearchDocument)
                .where(
                    sa.or_(
                        sa.and_(SearchDocument.kind == "note", ~sa.exists(live)),
                        sa.and_(
                            SearchDocument.kind == "source",
                            sa.or_(
                                ~eligible(SearchDocument.kind, SearchDocument.ref_id),
                                ~current_source(SearchDocument.ref_id, SearchDocument.version),
                            ),
                        ),
                    )
                )
                .returning(SearchDocument.ref_id)
            )
            counts["removed"] = len(removed.all())
            if counts["removed"]:
                await _changed(session, now)
            await session.execute(
                sa.update(SearchDocument)
                .where(
                    SearchDocument.kind == "note",
                    Note.id == SearchDocument.ref_id,
                    SearchDocument.path != Note.path,
                )
                .values(path=Note.path)
            )
        async with store.session_factory() as session:
            failures = {
                (row.kind, row.ref_id): row
                for row in (await session.scalars(sa.select(SearchUnreadable))).all()
            }
            stale_notes = (
                await session.execute(
                    sa.select(Note.id, Note.path, Note.content_hash)
                    .outerjoin(
                        SearchDocument,
                        sa.and_(SearchDocument.kind == "note", SearchDocument.ref_id == Note.id),
                    )
                    .where(
                        Note.state == "ok",
                        reviewed_note(),
                        sa.or_(
                            SearchDocument.ref_id.is_(None),
                            SearchDocument.version != Note.content_hash,
                        ),
                    )
                    # Randomize new attempts; known failures are filtered by
                    # their filesystem observation before consuming the batch.
                    .order_by(sa.func.random())
                )
            ).all()
            stale_sources = (
                await session.execute(
                    sa.select(Source.id, Source.current_revision)
                    # A normal pending projection is readable but unpublished.
                    # Leave it until review; orphaned sources still need a mark.
                    .where(
                        sa.or_(
                            sa.exists(
                                sa.select(NoteSource.source_id)
                                .join(Note, Note.id == NoteSource.note_id)
                                .where(
                                    NoteSource.source_id == Source.id,
                                    NoteSource.approved_revision == Source.current_revision,
                                    reviewed_note(),
                                )
                            ),
                            ~sa.exists(
                                sa.select(NoteSource.source_id)
                                .join(Note, Note.id == NoteSource.note_id)
                                .where(
                                    NoteSource.source_id == Source.id,
                                    Note.state.not_in(("missing", "deleted")),
                                )
                            ),
                        )
                    )
                    .outerjoin(
                        SearchDocument,
                        sa.and_(
                            SearchDocument.kind == "source", SearchDocument.ref_id == Source.id
                        ),
                    )
                    .where(
                        sa.or_(
                            SearchDocument.ref_id.is_(None),
                            SearchDocument.version != sa.func.concat("r", Source.current_revision),
                        )
                    )
                    .order_by(sa.func.random())
                )
            ).all()
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc

    def read_all() -> tuple[list[Entry], list[dict[str, Any]], int]:
        ready: list[Entry] = []
        failed: list[dict[str, Any]] = []
        skipped = 0
        for kind, rows in (("note", stale_notes), ("source", stale_sources)):
            attempted = 0
            for row in rows:
                version = (
                    row.content_hash if kind == "note" else source_version(row.current_revision)
                )
                previous = failures.get((kind, row.id))
                manifest_state = (
                    _file_state(store.sources_root / row.id / "manifest.json")
                    if kind == "source"
                    else ""
                )
                reasons: list[str] = []
                if kind == "note":
                    path = row.path
                elif previous and previous.observation.split("|", 1)[0] == manifest_state:
                    path = previous.path
                else:
                    path = _projection_path(store.sources_root, row.id, reasons) or ""
                observation = manifest_state + "|" + _relative_state(store.notes_root, path)
                if previous and (previous.version, previous.path, previous.observation) == (
                    version,
                    path,
                    observation,
                ):
                    skipped += 1
                    continue
                if attempted >= CATCH_UP_BATCH:
                    continue
                attempted += 1
                entry = (
                    _read_note(store.notes_root, path, row.id, version, id_key, reasons)
                    if kind == "note"
                    else _read_projection(
                        store.notes_root, path, row.id, row.current_revision, reasons
                    )
                    if path
                    else None
                )
                if entry is not None:
                    ready.append(entry)
                else:
                    failed.append(
                        dict(
                            kind=kind,
                            ref_id=row.id,
                            version=version,
                            path=path,
                            observation=observation,
                            observed_at=now,
                            reason=reasons[0]
                            if reasons
                            else "manifest has no readable projection path",
                        )
                    )
        return ready, failed, skipped

    ready, failed, skipped = await asyncio.to_thread(read_all)
    counts["unreadable"] = len(failed)
    counts["skipped_known_unreadable"] = skipped
    logged: list[dict[str, Any]] = []
    try:
        async with transaction(store.session_factory) as session:
            await session.execute(
                sa.delete(SearchUnreadable).where(
                    sa.or_(
                        sa.and_(
                            SearchUnreadable.kind == "note",
                            ~sa.exists(
                                sa.select(Note.id).where(
                                    Note.id == SearchUnreadable.ref_id, reviewed_note()
                                )
                            ),
                        ),
                        sa.and_(
                            SearchUnreadable.kind == "source",
                            ~sa.exists(
                                sa.select(Source.id).where(Source.id == SearchUnreadable.ref_id)
                            ),
                        ),
                    )
                )
            )
            for failure in failed:
                # A store write may have indexed newer bytes while this read
                # was in flight. Do not leave a failure over its successful row.
                kind, ref_id = failure["kind"], failure["ref_id"]
                mirrored = (
                    sa.exists(
                        sa.select(Note.id).where(
                            Note.id == ref_id,
                            Note.path == failure["path"],
                            Note.content_hash == failure["version"],
                            reviewed_note(),
                        )
                    )
                    if kind == "note"
                    else sa.exists(
                        sa.select(Source.id).where(
                            Source.id == ref_id,
                            sa.func.concat("r", Source.current_revision) == failure["version"],
                        )
                    )
                )
                indexed = sa.exists(
                    sa.select(SearchDocument.ref_id).where(
                        SearchDocument.kind == kind,
                        SearchDocument.ref_id == ref_id,
                        SearchDocument.version == failure["version"],
                    )
                )
                statement = insert(SearchUnreadable).from_select(
                    list(failure),
                    sa.select(*(sa.literal(value) for value in failure.values())).where(
                        mirrored, ~indexed
                    ),
                )
                changed = await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[SearchUnreadable.kind, SearchUnreadable.ref_id],
                        set_={
                            key: statement.excluded[key]
                            for key in failure
                            if key not in {"kind", "ref_id"}
                        },
                        where=sa.or_(
                            SearchUnreadable.version != statement.excluded.version,
                            SearchUnreadable.path != statement.excluded.path,
                            SearchUnreadable.observation != statement.excluded.observation,
                        ),
                    ).returning(SearchUnreadable.ref_id)
                )
                if changed.first() is not None:
                    logged.append(failure)
            for entry in ready:
                if entry.kind == "note":
                    current = await session.scalar(
                        sa.select(Note.content_hash).where(Note.id == entry.ref_id)
                    )
                    if current != entry.version:
                        continue
                await _write(session, entry, now)
                counts["indexed"] += 1
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
    for failure in logged:
        log.warning(
            "search entry unreadable",
            **{
                **failure,
                "path": failure["path"]
                or str(store.sources_root / failure["ref_id"] / "manifest.json"),
            },
        )
    return counts


def _walk(
    root: Path,
    live_notes: dict[str, str],
    sources_folder: tuple[str, ...],
    id_key: str,
    reviewed_key: str = "reviewed",
) -> Iterator[Entry]:
    """Every indexable file under the notes filesystem root.

    A file is a note when its frontmatter carries an identity the mirror holds
    as a live note, and the copy at the mirror's own path wins when two files
    carry one identity. A file is a source when it is generated output below
    the sources folder that names its source and revision.
    """
    notes: dict[str, Entry] = {}
    sources: dict[str, Entry] = {}
    for path in sorted(root.rglob("*.md")):
        relative_path = path.relative_to(root)
        if any(part in _IGNORED_DIRECTORIES for part in relative_path.parts):
            continue
        relative = relative_path.as_posix()
        try:
            if not path.is_file():
                continue
            data = resolve(root, relative).read_bytes()
            frontmatter, body = fm.parse(data.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, fm.FrontmatterError):
            continue
        carried = frontmatter.get(id_key)
        source_id = frontmatter.get("source_id")
        revision = frontmatter.get("source_revision")
        if (
            isinstance(carried, str)
            and carried in live_notes
            and frontmatter.get(reviewed_key) is True
        ):
            if carried in notes and notes[carried].path == live_notes[carried]:
                continue
            notes[carried] = Entry(
                "note", carried, relative, _title_of(body, relative), body, content_hash(data)
            )
        elif (
            sources_folder
            and relative_path.parts[: len(sources_folder)] == sources_folder
            and frontmatter.get("managed") is True
            and frontmatter.get("reviewed") is True
            and isinstance(source_id, str)
            and isinstance(revision, int)
            and source_id not in sources
        ):
            sources[source_id] = Entry(
                "source",
                source_id,
                relative,
                _title_of(body, relative),
                body,
                source_version(revision),
            )
    yield from notes.values()
    yield from sources.values()


async def rebuild(store: LocalStore) -> dict[str, int]:
    """Throw the index away and read it back from the notes filesystem.

    The one thing kept is the entry of a note the mirror holds as unparsed:
    its file cannot be read again, so its last indexed text is all there is.
    """
    started = datetime.now(tz=UTC)
    try:
        async with transaction(store.session_factory) as session:
            await session.execute(sa.delete(SearchUnreadable))
            await session.execute(
                sa.update(SearchIndexState)
                .where(SearchIndexState.id == 1)
                .values(rebuild_started_at=started, rebuild_error=None)
            )
        async with store.session_factory() as session:
            live_notes = {
                row.id: row.path
                for row in (
                    await session.execute(sa.select(Note.id, Note.path).where(reviewed_note()))
                ).all()
            }
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
    settings = store.control.settings()
    sources_folder = tuple(
        part for part in sanitize_folder(settings.notes.sources_folder).split("/") if part
    )
    id_key = store.control.schema().role("id_key")
    entries = await asyncio.to_thread(
        lambda: list(
            _walk(
                store.notes_root,
                live_notes,
                sources_folder,
                id_key,
                store.control.schema().role("reviewed_key"),
            )
        )
    )
    now = datetime.now(tz=UTC)
    try:
        async with transaction(store.session_factory) as session:
            # A note the mirror holds as unparsed stays findable by the text
            # it was last indexed with, which the walk could not read again.
            walked = [entry.ref_id for entry in entries if entry.kind == "note"]
            unparsed = sa.select(Note.id).where(
                Note.state == "unparsed", reviewed_note(), Note.id.not_in(walked)
            )
            await session.execute(
                sa.delete(SearchDocument).where(
                    sa.not_(
                        sa.and_(SearchDocument.kind == "note", SearchDocument.ref_id.in_(unparsed))
                    )
                )
            )
            for start in range(0, len(entries), _REBUILD_BATCH):
                batch = entries[start : start + _REBUILD_BATCH]
                # A store write that commits while this runs has indexed newer
                # bytes than the walk read, so its entry is kept, not replaced.
                for entry in batch:
                    await _write(session, entry, now)
            await session.execute(
                sa.update(SearchIndexState)
                .where(SearchIndexState.id == 1)
                .values(last_changed_at=now, rebuild_completed_at=now)
            )
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
    # A store write that committed while the walk was reading may have been
    # replaced by the older bytes the walk saw; the mirror knows better.
    await catch_up(store)
    current = await status(store)
    return {"notes": current.notes, "sources": current.sources}


def _row(entry: Entry, now: datetime) -> dict[str, Any]:
    return {
        "kind": entry.kind,
        "ref_id": entry.ref_id,
        "path": entry.path,
        "title": indexable(entry.title),
        "body": indexable(entry.body),
        "version": entry.version,
        "indexed_at": now,
    }


async def record_rebuild_failure(store: LocalStore, reason: str) -> None:
    """Say on the Admin page that the last rebuild did not finish."""
    try:
        async with transaction(store.session_factory) as session:
            await session.execute(
                sa.update(SearchIndexState)
                .where(SearchIndexState.id == 1)
                .values(rebuild_error=reason)
            )
    except (SQLAlchemyError, OSError):
        log.warning("search index rebuild failure could not be recorded", reason=reason)


async def never_built(store: LocalStore) -> bool:
    """Whether no rebuild has ever completed, which is true once after the upgrade."""
    async with store.session_factory() as session:
        state = await session.get(SearchIndexState, 1)
    return state is None or state.rebuild_completed_at is None


async def after_pass(store: LocalStore) -> dict[str, int]:
    """What the reconciler runs once a pass has updated the mirror."""
    if await never_built(store) and not store.search_rebuild_running():
        return await rebuild(store)
    return await catch_up(store)


async def start_rebuild(store: LocalStore) -> RebuildSearchIndexResult:
    started = store.start_search_rebuild()
    return RebuildSearchIndexResult(started=started, status=await status(store))
