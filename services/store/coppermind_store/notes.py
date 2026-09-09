"""Creating and reading notes: the local implementation of the store contract.

Write protocol, in this order, for every mutation:

1. Validate against the frontmatter schema and choose the target path.
2. Open the metadata transaction. A PostgreSQL outage stops here, before
   anything is written, so a write returns a clean 503 and leaves no orphan
   file behind.
3. Create the file exclusively, fsync it, and hash the bytes that landed.
4. Upsert the metadata row and commit.

The commit is last on purpose: a crash between steps 3 and 4 leaves the file
on disk with no row, and the filesystem is the truth, so reconciliation brings
PostgreSQL up to date on its next pass. It never leaves a row pointing at a
file that does not exist, and a client retry cannot create a duplicate note
for the same write.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coppermind import frontmatter as fm
from coppermind.atomicio import create_exclusive_bytes
from coppermind.db.models import Note
from coppermind.db.session import transaction
from coppermind.ids import new_id
from coppermind.naming import note_stem, sanitize_folder, unique_stem
from coppermind.schema import FrontmatterSchema
from coppermind.settings import ProductSettings
from coppermind.store_protocol import (
    CreateNote,
    MetadataUnavailable,
    NoteDocument,
    NoteId,
    NotesFilesystemUnavailable,
    NotFound,
    PathCollision,
    RawNote,
    ValidationFailed,
)
from coppermind_store.control import ControlState
from coppermind_store.fs import NOTE_SUFFIX, content_hash, existing_stems, resolve


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

    async def create_note(self, request: CreateNote) -> NoteDocument:
        settings = self.control.settings()
        schema = self.control.schema()

        note_id = new_id()
        frontmatter = _build_frontmatter(request, schema, settings, note_id)
        problems = schema.validate_frontmatter(frontmatter)
        if problems:
            raise ValidationFailed(problems)

        folder = sanitize_folder(request.folder or settings.notes.review_folder)
        folder_path = resolve(self.notes_root, folder)
        stem = _stem_for(request.title, frontmatter, schema, settings)
        stem = unique_stem(stem, existing_stems(folder_path))
        relative = f"{folder}/{stem}{NOTE_SUFFIX}" if folder else f"{stem}{NOTE_SUFFIX}"
        target = folder_path / f"{stem}{NOTE_SUFFIX}"

        text = fm.compose(frontmatter, _body_with_heading(request.title, request.body))
        data = text.encode("utf-8")

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
                        schema_version=int(frontmatter.get(schema.role("schema_version_key"), 1)),
                        type=_text(frontmatter.get(schema.role("type_key"))),
                        context=_text(frontmatter.get(schema.role("context_key"))),
                        account=_text(frontmatter.get(schema.role("account_key"))),
                        date=_as_date(frontmatter.get(schema.role("date_key"))),
                        reviewed=bool(frontmatter.get(schema.role("reviewed_key"), False)),
                        tags=[str(t) for t in frontmatter.get(schema.role("tags_key"), []) or []],
                        state="ok",
                        first_seen_at=now,
                        updated_at=now,
                    )
                )
        except IntegrityError as exc:
            # The path is unique in the mirror, so this is a row that outlived
            # its file: the note was deleted on a device and no reconciler has
            # cleared the row yet. PostgreSQL is healthy, so saying otherwise
            # would send the operator after the wrong thing.
            raise PathCollision(relative) from exc
        except (SQLAlchemyError, OSError) as exc:
            # A connection refused by asyncpg arrives here as a bare OSError.
            # The filesystem write raises its own typed error above, so what
            # is left at this level is the database and only the database.
            raise MetadataUnavailable(str(exc)) from exc

        return NoteDocument(
            id=note_id,
            path=relative,
            title=request.title,
            frontmatter=_jsonable(frontmatter),
            body=_body_with_heading(request.title, request.body),
            content_hash=digest,
            size_bytes=len(data),
            updated_at=now,
            sources=[str(s) for s in frontmatter.get(schema.role("sources_key"), []) or []],
        )

    async def get_note(self, note_id: NoteId) -> NoteDocument:
        relative, path = await self._locate(note_id)
        data = path.read_bytes()
        text = data.decode("utf-8")
        frontmatter, body = fm.parse(text)
        schema = self.control.schema()
        return NoteDocument(
            id=note_id,
            path=relative,
            title=_title_of(body, path),
            frontmatter=_jsonable(frontmatter),
            body=body,
            content_hash=content_hash(data),
            size_bytes=len(data),
            updated_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
            sources=[str(s) for s in frontmatter.get(schema.role("sources_key"), []) or []],
        )

    async def read_raw(self, note_id: NoteId) -> RawNote:
        relative, path = await self._locate(note_id)
        data = path.read_bytes()
        return RawNote(
            id=note_id,
            path=relative,
            text=data.decode("utf-8"),
            content_hash=content_hash(data),
        )

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
        path = resolve(self.notes_root, relative)
        if not path.is_file():
            # The row outlived the file, which happens when a note is deleted
            # on a device. Reconciliation clears the row; until then, this is
            # honestly a miss rather than a server error.
            raise NotFound(note_id)
        return relative, path


def _build_frontmatter(
    request: CreateNote,
    schema: FrontmatterSchema,
    settings: ProductSettings,
    note_id: str,
) -> dict[str, Any]:
    """Merge the caller's frontmatter over the schema defaults, in key order."""
    supplied = dict(request.frontmatter)
    values: dict[str, Any] = schema.defaults()
    values.update(supplied)
    values[schema.role("id_key")] = note_id
    values.setdefault(schema.role("schema_version_key"), 1)
    date_key = schema.role("date_key")
    if not values.get(date_key):
        values[date_key] = _today(settings).isoformat()

    ordered: dict[str, Any] = {}
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
        if key not in ordered:
            ordered[key] = value
    return ordered


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
    return composed if composed.endswith("\n") else composed + "\n"


def _title_of(body: str, path: Path) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def _today(settings: ProductSettings) -> date:
    """Today in the operator's timezone.

    An unknown zone falls back to UTC rather than refusing the write: a note
    with a date one day out is recoverable, a rejected note is not.
    """
    zone: tzinfo
    try:
        zone = ZoneInfo(settings.general.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    return datetime.now(tz=zone).date()


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
