"""Creating, reading and replacing notes: the local implementation of the store contract.

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
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    SQLAlchemyError,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coppermind import frontmatter as fm
from coppermind.atomicio import commit_staged, create_exclusive_bytes, stage_bytes
from coppermind.db.models import Note
from coppermind.db.session import transaction
from coppermind.ids import new_id
from coppermind.logging import get_logger
from coppermind.naming import note_stem, sanitize_folder, unique_stem
from coppermind.schema import FrontmatterSchema
from coppermind.settings import ProductSettings
from coppermind.store_protocol import (
    CreateNote,
    ETag,
    MetadataUnavailable,
    NoteDocument,
    NoteId,
    NotesFilesystemUnavailable,
    NoteUnparseable,
    NotFound,
    PathCollision,
    ReplaceNote,
    StoreError,
    ValidationFailed,
    VersionConflict,
)
from coppermind_store.control import ControlState
from coppermind_store.fs import NOTE_SUFFIX, content_hash, existing_stems, is_note_file, resolve

log = get_logger("coppermind-store")


class LocalStore:
    """The store contract, implemented against a real notes filesystem."""

    def __init__(
        self,
        notes_root: Path,
        control: ControlState,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.notes_root = notes_root
        self.control = control
        self.session_factory = session_factory
        # One lock per note that has been written through this process. The
        # store is exactly one process, which is what makes an in-process lock
        # a sufficient guard for the compare-and-swap.
        self._locks: dict[str, asyncio.Lock] = {}

    async def create_note(self, request: CreateNote) -> NoteDocument:
        settings = self.control.settings()
        schema = self.control.schema()

        note_id = new_id()
        frontmatter = _build_frontmatter(request, schema, settings, note_id)
        problems = schema.validate_frontmatter(frontmatter)
        if problems:
            raise ValidationFailed(problems)
        sources = frontmatter.get(schema.role("sources_key"), [])

        folder = sanitize_folder(settings.notes.review_folder)
        folder_path = resolve(self.notes_root, folder)
        stem = _stem_for(request.title, frontmatter, schema, settings)
        stem = unique_stem(stem, existing_stems(folder_path))
        relative = f"{folder}/{stem}{NOTE_SUFFIX}" if folder else f"{stem}{NOTE_SUFFIX}"
        target = folder_path / f"{stem}{NOTE_SUFFIX}"

        text = fm.compose(frontmatter, _body_with_heading(request.title, request.body))
        data = text.encode("utf-8")

        created = False
        try:
            async with transaction(self.session_factory) as session:
                # Force the connection before touching the filesystem. SQLAlchemy
                # connects lazily, so without this a PostgreSQL outage would only
                # surface at commit, after the file had already been created.
                await session.execute(sa.text("SELECT 1"))
                try:
                    create_exclusive_bytes(target, data)
                except FileExistsError as exc:
                    raise PathCollision(relative) from exc
                except OSError as exc:
                    raise NotesFilesystemUnavailable(str(exc)) from exc
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
        except BaseException as exc:
            if isinstance(exc, IntegrityError):
                if created:
                    target.unlink(missing_ok=True)
                # The path is unique in the mirror, so this is a row that
                # outlived its file: the note was deleted on a device and no
                # reconciler has cleared the row yet. PostgreSQL is healthy,
                # so saying otherwise would send the operator after the wrong
                # thing.
                raise PathCollision(relative) from exc
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
            body=_body_with_heading(request.title, request.body),
            content_hash=digest,
            size_bytes=len(data),
            updated_at=now,
            sources=[str(source) for source in sources] if isinstance(sources, list) else [],
        )

    async def get_note(self, note_id: NoteId) -> NoteDocument:
        relative, path = await self._locate(note_id)
        data, mtime = _read(note_id, path)
        schema = self.control.schema()
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
            current = _frontmatter_at(note_id, relative, path, schema, if_match)
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
                    _replace_if_unchanged(note_id, relative, path, data, schema, if_match)
            except BaseException as exc:
                typed = _metadata_failure(exc)
                if typed is None:
                    raise
                raise typed from exc

        return NoteDocument(
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

    def _lock_for(self, note_id: NoteId) -> asyncio.Lock:
        """The lock serialising writes to one note. Only asked for a located note."""
        lock = self._locks.get(note_id)
        if lock is None:
            lock = self._locks[note_id] = asyncio.Lock()
        return lock

    async def _locate(self, note_id: NoteId) -> tuple[str, Path]:
        """Return the note's relative path and its absolute path.

        The path lives in PostgreSQL, so this is one of the reads that a
        database outage turns into a 503. The captain's rule applies: the
        notes filesystem stays fully usable and Obsidian keeps syncing, but
        the API does not invent state to keep answering.
        """
        try:
            async with self.session_factory() as session:
                relative = (
                    await session.execute(sa.select(Note.path).where(Note.id == note_id))
                ).scalar_one_or_none()
        except (SQLAlchemyError, OSError) as exc:
            raise MetadataUnavailable(str(exc)) from exc
        if relative is None:
            raise NotFound(note_id)
        try:
            path = resolve(self.notes_root, relative)
        except ValueError as exc:
            # The mirrored path leaves the notes filesystem, so the store
            # refuses to follow it. Nothing servable is there.
            raise NotFound(note_id) from exc
        if not is_note_file(path):
            # The row outlived the file, which happens when a note is deleted
            # on a device. Reconciliation clears the row; until then, this is
            # honestly a miss rather than a server error.
            raise NotFound(note_id)
        return relative, path


def _parse(
    note_id: NoteId, relative: str, data: bytes, schema: FrontmatterSchema
) -> tuple[dict[str, Any], str]:
    """Parse a located note's bytes and check they are that note's."""
    try:
        frontmatter, body = fm.parse(data.decode("utf-8"))
    except (fm.FrontmatterError, UnicodeDecodeError) as exc:
        # A person broke this file on a device. That is not a fault of the
        # store, and the answer says so rather than blaming Coppermind.
        # The parser's reason quotes the offending lines, so it is the
        # person's own note content: it reaches the internal surface, which
        # the store alone answers, and never the log or the public
        # envelope. Logs are collected and shipped, and an unauthenticated
        # read can trigger this, so the log gets only what the type of the
        # failure and its position say.
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
        _frontmatter_at(note_id, relative, path, schema, if_match)
        commit_staged(staged, path)
    except OSError as exc:
        staged.unlink(missing_ok=True)
        raise NotesFilesystemUnavailable(str(exc)) from exc
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _frontmatter_at(
    note_id: NoteId, relative: str, path: Path, schema: FrontmatterSchema, if_match: ETag
) -> dict[str, Any]:
    """The frontmatter of a located note whose file still hashes to `if_match`.

    The identity check comes before the compare, so a row whose file is now
    another note is a miss whatever ETag was sent, not a conflict naming the
    other note's hash.
    """
    current, _ = _read(note_id, path)
    frontmatter, _ = _parse(note_id, relative, current, schema)
    current_hash = content_hash(current)
    if current_hash != if_match:
        raise VersionConflict(current_hash)
    return frontmatter


def _read(note_id: NoteId, path: Path) -> tuple[bytes, float]:
    """The bytes and mtime of a located note, with typed filesystem failures."""
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


def _build_frontmatter(
    request: CreateNote,
    schema: FrontmatterSchema,
    settings: ProductSettings,
    note_id: str,
) -> dict[str, Any]:
    """Merge the caller's frontmatter over the schema defaults, in key order."""
    values: dict[str, Any] = schema.defaults()
    values.update(request.frontmatter)
    values[schema.role("id_key")] = note_id
    values.setdefault(schema.role("schema_version_key"), 1)
    date_key = schema.role("date_key")
    if not values.get(date_key):
        values[date_key] = _today(settings).isoformat()
    return _ordered(values, schema)


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


def _ordered(values: dict[str, Any], schema: FrontmatterSchema) -> dict[str, Any]:
    """Known keys in schema order with their kinds applied, then the rest as given."""
    ordered: dict[str, Any] = {}
    known_keys = {definition.name for definition in schema.keys}
    for definition in schema.keys:
        if definition.name in values and values[definition.name] is not None:
            value = values[definition.name]
            # A date key is written as a YAML date, not a quoted string, so the
            # file reads the way a person would write it in Obsidian and a
            # round trip through the store does not add quotes to it.
            if definition.kind == "date":
                value = _as_date(value) or value
            ordered[definition.name] = value
    # Keys the schema does not know about are kept, after the known ones.
    for key, value in values.items():
        if key not in known_keys:
            ordered[key] = value
    return ordered


def _mirror_columns(frontmatter: dict[str, Any], schema: FrontmatterSchema) -> dict[str, Any]:
    """The columns of the mirror row that project individual frontmatter keys."""
    tags = frontmatter.get(schema.role("tags_key"), [])
    return {
        "schema_version": _as_int(frontmatter.get(schema.role("schema_version_key"), 1)),
        "type": _text(frontmatter.get(schema.role("type_key"))),
        "context": _text(frontmatter.get(schema.role("context_key"))),
        "account": _text(frontmatter.get(schema.role("account_key"))),
        "date": _as_date(frontmatter.get(schema.role("date_key"))),
        "reviewed": bool(frontmatter.get(schema.role("reviewed_key"), False)),
        "tags": [str(tag) for tag in tags] if isinstance(tags, list) else [],
    }


def _stem_for(
    title: str,
    frontmatter: dict[str, Any],
    schema: FrontmatterSchema,
    settings: ProductSettings,
) -> str:
    note_type = _text(frontmatter.get(schema.role("type_key")))
    dated = note_type in settings.notes.dated_types
    note_date = _as_date(frontmatter.get(schema.role("date_key")))
    return note_stem(title, note_date=note_date, dated=dated)


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
            return line[2:].strip()
    return path.stem


def _today(settings: ProductSettings) -> date:
    """Today in the operator's timezone."""
    return datetime.now(tz=ZoneInfo(settings.general.timezone)).date()


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


def _jsonable(value: Any) -> Any:
    """Convert a round tripped YAML mapping into plain JSON friendly types."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value
