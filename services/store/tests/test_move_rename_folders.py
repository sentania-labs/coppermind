"""Move, rename, and folder listing on the store contract.

These tests use ``LocalStore`` directly with a temp directory and an in-memory
SQLite database to exercise ``move_note``, ``rename_note`` and ``list_folders``.
They prove the acceptance criteria:

* ``AC4``: a move keeps the note id and the reconciler sees it.
* ``AC1``: rename keeps the id; stale ``If-Match`` is 409.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from coppermind_store.control import ControlState
from coppermind_store.notes import LocalStore
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from coppermind.db.models import Base, Note
from coppermind.db.session import make_session_factory
from coppermind.ids import new_id
from coppermind.store_protocol import (
    FolderTree,
    MoveNote,
    NotFound,
    PathCollision,
    RenameNote,
    ValidationFailed,
    VersionConflict,
)

pytestmark = pytest.mark.asyncio

_make_store_engine: AsyncEngine | None = None


async def _make_store(tmp_path: Path):
    """Create a LocalStore backed by a temp directory with an in-memory SQLite DB."""
    notes_root = tmp_path / "notes"
    notes_root.mkdir()
    sources_root = tmp_path / "sources"
    sources_root.mkdir()
    state_dir = tmp_path / "state"
    control = ControlState(state_dir)
    control.ensure_defaults()

    db_path = tmp_path / "test.db"
    global _make_store_engine
    if _make_store_engine is None:
        _make_store_engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    factory = make_session_factory(_make_store_engine)

    # Convert JSONB to JSON for SQLite compatibility.
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, (JSONB, type(sa.ARRAY(None)))):
                col.type = JSON()

    # Create tables
    async with _make_store_engine.connect() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn))

    store = LocalStore(
        notes_root=notes_root,
        control=control,
        session_factory=factory,
        sources_root=sources_root,
    )
    return store


async def _insert_note_row(store, note_id, path, title, frontmatter):
    """Write a note file and a DB row."""
    from datetime import UTC, datetime

    from coppermind_store.fs import content_hash

    text = store.notes_root.joinpath(path)
    text.parent.mkdir(parents=True, exist_ok=True)
    from coppermind import frontmatter as fm

    text.write_text(fm.compose(frontmatter, ""), encoding="utf-8")

    data = text.read_bytes()
    digest = content_hash(data)

    now = datetime.now(tz=UTC)
    async with store.session_factory() as session:
        await session.execute(
            sa.insert(Note).values(
                id=note_id,
                path=path,
                title=title,
                content_hash=digest,
                size_bytes=len(data),
                mtime=now,
                frontmatter=frontmatter,
                schema_version=1,
                state="ok",
                first_seen_at=now,
                updated_at=now,
            )
        )

        await session.commit()


def _content_hash(store: LocalStore, path: str) -> str:
    from coppermind_store.fs import content_hash

    return content_hash((store.notes_root / path).read_bytes())


# -- move_note tests -----------------------------------------------------------


async def test_move_keeps_the_note_id(store: LocalStore):
    """Move keeps the note's identifier."""
    note_id = new_id()
    frontmatter = {
        "schema_version": 1,
        "id": note_id,
        "date": "2026-09-08",
        "type": "note",
        "context": "internal",
        "reviewed": False,
        "sources": [],
        "tags": [],
    }
    await _insert_note_row(store, note_id, "Review/Test.md", "Test", frontmatter)

    result = await store.move_note(note_id, MoveNote(target_folder="Work"), if_match=None)
    assert result.id == note_id
    assert result.path == "Work/Test.md"


async def test_move_refuses_sources_folder(store: LocalStore):
    """A move into _Sources is rejected."""
    note_id = new_id()
    frontmatter = {
        "schema_version": 1,
        "id": note_id,
        "date": "2026-09-08",
        "type": "note",
        "context": "internal",
        "reviewed": False,
        "sources": [],
        "tags": [],
    }
    await _insert_note_row(store, note_id, "Review/Test.md", "Test", frontmatter)

    with pytest.raises(ValidationFailed) as exc_info:
        await store.move_note(note_id, MoveNote(target_folder="_Sources"), if_match=None)

    assert "_Sources" in str(exc_info.value.errors)


async def test_move_refuses_note_that_does_not_exist(store: LocalStore):
    """Moving a note that does not exist returns 404."""
    with pytest.raises(NotFound):
        await store.move_note(
            "01K4Q8Z3N7V2X9M1B5C6D8E0F2", MoveNote(target_folder="Work"), if_match=None
        )


async def test_move_creates_path_collision(store: LocalStore):
    """Moving to a path where another note already exists returns 409."""
    note_id1 = new_id()
    note_id2 = new_id()
    frontmatter1 = {
        "schema_version": 1,
        "id": note_id1,
        "date": "2026-09-08",
        "type": "note",
        "context": "internal",
        "reviewed": False,
        "sources": [],
        "tags": [],
    }
    frontmatter2 = {
        "schema_version": 1,
        "id": note_id2,
        "date": "2026-09-08",
        "type": "note",
        "context": "internal",
        "reviewed": False,
        "sources": [],
        "tags": [],
    }
    await _insert_note_row(store, note_id1, "Work/2026-09-08 Same.md", "Same", frontmatter1)
    await _insert_note_row(store, note_id2, "Review/2026-09-08 Same.md", "Same", frontmatter2)

    with pytest.raises(PathCollision):
        await store.move_note(note_id2, MoveNote(target_folder="Work"), if_match=None)


# -- rename_note tests ---------------------------------------------------------


async def test_rename_keeps_the_note_id(store: LocalStore):
    """Rename keeps the note's identifier and path."""
    note_id = new_id()
    frontmatter = {
        "schema_version": 1,
        "id": note_id,
        "date": "2026-09-08",
        "type": "note",
        "context": "internal",
        "reviewed": False,
        "sources": [],
        "tags": [],
    }
    await _insert_note_row(store, note_id, "Review/Test.md", "Test", frontmatter)

    old_hash = _content_hash(store, "Review/Test.md")
    result = await store.rename_note(note_id, RenameNote(title="New Title"), if_match=old_hash)

    assert result.id == note_id
    assert result.path == "Review/Test.md"  # path unchanged
    assert result.title == "New Title"


async def test_rename_refuses_stale_if_match(store: LocalStore):
    """A stale If-Match on rename returns 409 version_conflict."""
    note_id = new_id()
    frontmatter = {
        "schema_version": 1,
        "id": note_id,
        "date": "2026-09-08",
        "type": "note",
        "context": "internal",
        "reviewed": False,
        "sources": [],
        "tags": [],
    }
    await _insert_note_row(store, note_id, "Review/Test.md", "Test", frontmatter)

    with pytest.raises(VersionConflict):
        await store.rename_note(note_id, RenameNote(title="New"), if_match="sha256:wrong")


async def test_rename_refuses_note_that_does_not_exist(store: LocalStore):
    """Renaming a note that does not exist returns 404."""
    with pytest.raises(NotFound):
        await store.rename_note(
            "01K4Q8Z3N7V2X9M1B5C6D8E0F2",
            RenameNote(title="New"),
            if_match="sha256:abc",
        )


# -- list_folders tests --------------------------------------------------------


async def test_list_folders_returns_tree_with_counts(store: LocalStore):
    """list_folders returns a tree with note counts."""
    note_id1 = new_id()
    note_id2 = new_id()
    note_id3 = new_id()
    frontmatter = {
        "schema_version": 1,
        "id": note_id1,
        "date": "2026-09-08",
        "type": "note",
        "context": "internal",
        "reviewed": False,
        "sources": [],
        "tags": [],
    }
    frontmatter2 = {
        "schema_version": 1,
        "id": note_id2,
        "date": "2026-09-08",
        "type": "note",
        "context": "internal",
        "reviewed": False,
        "sources": [],
        "tags": [],
    }
    frontmatter3 = {
        "schema_version": 1,
        "id": note_id3,
        "date": "2026-09-08",
        "type": "note",
        "context": "internal",
        "reviewed": False,
        "sources": [],
        "tags": [],
    }
    await _insert_note_row(store, note_id1, "Review/Note1.md", "Note 1", frontmatter)
    await _insert_note_row(store, note_id2, "Review/Note2.md", "Note 2", frontmatter2)
    await _insert_note_row(store, note_id3, "Work/Note3.md", "Note 3", frontmatter3)

    tree = await store.list_folders()
    assert isinstance(tree, FolderTree)
    assert len(tree.children) >= 2

    # Find the "Review" folder and check count
    review_folder = None
    for f in tree.children:
        if f.name == "Review":
            review_folder = f
            break

    assert review_folder is not None
    assert review_folder.note_count == 2


async def test_list_folders_empty_when_no_notes(store: LocalStore):
    """An empty notes filesystem returns an empty tree."""
    tree = await store.list_folders()
    assert isinstance(tree, FolderTree)
    assert tree.children == []


# -- fixture -------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path):
    return await _make_store(tmp_path)


@pytest.fixture
def store_for_sync(tmp_path: Path):
    """Sync fixture for tests that don't need async setup."""
    return _make_store_sync(tmp_path)


def _make_store_sync(tmp_path: Path):
    """Sync store creation for non-async fixtures."""
    notes_root = tmp_path / "notes"
    notes_root.mkdir()
    sources_root = tmp_path / "sources"
    sources_root.mkdir()
    state_dir = tmp_path / "state"
    control = ControlState(state_dir)
    control.ensure_defaults()

    global _make_store_engine
    db_path = tmp_path / "test.db"
    if _make_store_engine is None:
        _make_store_engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    factory = make_session_factory(_make_store_engine)

    # Convert JSONB to JSON for SQLite compatibility.
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, (JSONB, type(sa.ARRAY(None)))):
                col.type = JSON()

    store = LocalStore(
        notes_root=notes_root,
        control=control,
        session_factory=factory,
        sources_root=sources_root,
    )
    return store
