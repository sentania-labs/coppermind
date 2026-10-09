"""The full-text index: kept current from the metadata mirror, rebuilt from files.

Every row of `search_documents` is derived. A note's entry is written in the
same transaction as the mirror row a store write or a reconciliation pass
records, so the index changes when the mirror does and a rolled back write
leaves no entry behind. Generated source projections are indexed after the
ingest that wrote them, and a catch-up step after every reconciliation pass
indexes anything the mirror knows that the index does not yet describe: a
note an ingest opened, a write that crashed after its file landed, or the
whole notes filesystem the first time the index exists.

The rebuild job throws every entry away and reads the notes filesystem again,
which is the recovery story: lose the table and one job puts it back. It reads
files, not the mirror's copy of anything, and asks the mirror only which
identities are live notes, because a file carrying an identity the mirror has
not adopted yet is not a note until reconciliation says it is.
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
from coppermind.db.models import Note, SearchDocument, SearchIndexState, Source
from coppermind.db.session import transaction
from coppermind.logging import get_logger
from coppermind.naming import sanitize_folder
from coppermind.store_protocol import (
    MetadataUnavailable,
    RebuildSearchIndexResult,
    SearchIndexStatus,
    StoreError,
)
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
    statement = insert(SearchDocument).values(**values)
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
            newest = await session.scalar(sa.select(sa.func.max(SearchDocument.indexed_at)))
            state = await session.get(SearchIndexState, 1)
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
    times = [newest]
    if state is not None:
        times += [state.last_changed_at, state.rebuild_completed_at]
    known = [moment for moment in times if moment is not None]
    return SearchIndexStatus(
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
    root: Path, relative: str, note_id: str, expected_hash: str, id_key: str
) -> Entry | None:
    """The entry for a mirrored note, or None if the file is not that note now."""
    try:
        data = resolve(root, relative).read_bytes()
    except (OSError, ValueError):
        return None
    if content_hash(data) != expected_hash:
        # The file moved on since the mirror read it; the next pass mirrors the
        # new bytes and the entry follows from there.
        return None
    try:
        frontmatter, body = fm.parse(data.decode("utf-8"))
    except (UnicodeDecodeError, fm.FrontmatterError):
        return None
    if str(frontmatter.get(id_key, note_id)) != note_id:
        return None
    return Entry("note", note_id, relative, _title_of(body, relative), body, expected_hash)


def _read_projection(root: Path, relative: str, source_id: str, revision: int) -> Entry | None:
    """The entry for a source's generated page, if the path still holds it."""
    try:
        text = resolve(root, relative).read_text(encoding="utf-8")
        frontmatter, body = fm.parse(text)
    except (OSError, ValueError, UnicodeDecodeError, fm.FrontmatterError):
        return None
    if (
        frontmatter.get("managed") is not True
        or frontmatter.get("source_id") != source_id
        or frontmatter.get("source_revision") != revision
    ):
        return None
    return Entry(
        "source", source_id, relative, _title_of(body, relative), body, source_version(revision)
    )


def _projection_path(sources_root: Path, source_id: str) -> str | None:
    """The page a source's manifest records, the only record of where it lives."""
    from coppermind_store.sources import _manifest_document

    try:
        manifest = _manifest_document(sources_root / source_id / "manifest.json")
    except (OSError, StoreError):
        return None
    recorded = manifest.get("projection_path")
    return recorded if isinstance(recorded, str) and recorded else None


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


async def catch_up(store: LocalStore) -> dict[str, int]:
    """Bring the index level with the mirror, a bounded batch at a time.

    Entries for notes that are gone are removed, entries whose note moved
    while its file could not be read take the new path, and up to
    `CATCH_UP_BATCH` notes and source projections the index describes at an
    older version, or not at all, are read from their files and indexed.
    """
    counts = {"indexed": 0, "removed": 0, "unreadable": 0}
    id_key = store.control.schema().role("id_key")
    now = datetime.now(tz=UTC)
    live = sa.select(Note.id).where(Note.id == SearchDocument.ref_id, Note.state != "missing")
    try:
        async with transaction(store.session_factory) as session:
            removed = await session.execute(
                sa.delete(SearchDocument)
                .where(SearchDocument.kind == "note", ~sa.exists(live))
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
            stale_notes = (
                await session.execute(
                    sa.select(Note.id, Note.path, Note.content_hash)
                    .outerjoin(
                        SearchDocument,
                        sa.and_(SearchDocument.kind == "note", SearchDocument.ref_id == Note.id),
                    )
                    .where(
                        Note.state == "ok",
                        sa.or_(
                            SearchDocument.ref_id.is_(None),
                            SearchDocument.version != Note.content_hash,
                        ),
                    )
                    # Random rather than by identifier, so a batch of files
                    # that cannot be read this pass never starves the rest.
                    .order_by(sa.func.random())
                    .limit(CATCH_UP_BATCH)
                )
            ).all()
            stale_sources = (
                await session.execute(
                    sa.select(Source.id, Source.current_revision)
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
                    .limit(CATCH_UP_BATCH)
                )
            ).all()
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc

    def read_all() -> list[Entry | None]:
        found: list[Entry | None] = [
            _read_note(store.notes_root, row.path, row.id, row.content_hash, id_key)
            for row in stale_notes
        ]
        for row in stale_sources:
            page = _projection_path(store.sources_root, row.id)
            found.append(
                None
                if page is None
                else _read_projection(store.notes_root, page, row.id, row.current_revision)
            )
        return found

    entries = await asyncio.to_thread(read_all)
    ready = [entry for entry in entries if entry is not None]
    counts["unreadable"] = len(entries) - len(ready)
    if ready:
        try:
            async with transaction(store.session_factory) as session:
                for entry in ready:
                    if entry.kind == "note":
                        # Index only the bytes the mirror still describes: a
                        # store write that landed since the read has already
                        # indexed newer ones in its own transaction.
                        current = await session.scalar(
                            sa.select(Note.content_hash).where(Note.id == entry.ref_id)
                        )
                        if current != entry.version:
                            continue
                    await _write(session, entry, now)
                    counts["indexed"] += 1
        except (SQLAlchemyError, OSError) as exc:
            raise MetadataUnavailable(str(exc)) from exc
    return counts


def _walk(
    root: Path, live_notes: dict[str, str], sources_folder: tuple[str, ...], id_key: str
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
        if isinstance(carried, str) and carried in live_notes:
            if carried in notes and notes[carried].path == live_notes[carried]:
                continue
            notes[carried] = Entry(
                "note", carried, relative, _title_of(body, relative), body, content_hash(data)
            )
        elif (
            sources_folder
            and relative_path.parts[: len(sources_folder)] == sources_folder
            and frontmatter.get("managed") is True
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
            await session.execute(
                sa.update(SearchIndexState)
                .where(SearchIndexState.id == 1)
                .values(rebuild_started_at=started, rebuild_error=None)
            )
        async with store.session_factory() as session:
            live_notes = {
                row.id: row.path
                for row in (
                    await session.execute(
                        sa.select(Note.id, Note.path).where(Note.state != "missing")
                    )
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
        lambda: list(_walk(store.notes_root, live_notes, sources_folder, id_key))
    )
    now = datetime.now(tz=UTC)
    try:
        async with transaction(store.session_factory) as session:
            # A note the mirror holds as unparsed stays findable by the text
            # it was last indexed with, which the walk could not read again.
            walked = [entry.ref_id for entry in entries if entry.kind == "note"]
            unparsed = sa.select(Note.id).where(Note.state == "unparsed", Note.id.not_in(walked))
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
                await session.execute(
                    insert(SearchDocument).on_conflict_do_nothing(),
                    [_row(entry, now) for entry in batch],
                )
            await session.execute(
                sa.update(SearchIndexState)
                .where(SearchIndexState.id == 1)
                .values(last_changed_at=now, rebuild_completed_at=now)
            )
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
    counts = {
        "notes": sum(1 for entry in entries if entry.kind == "note"),
        "sources": sum(1 for entry in entries if entry.kind == "source"),
    }
    # A store write that committed while the walk was reading may have been
    # replaced by the older bytes the walk saw; the mirror knows better.
    await catch_up(store)
    return counts


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
