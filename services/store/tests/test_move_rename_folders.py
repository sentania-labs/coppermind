"""Database-free checks for move and rename filesystem behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from coppermind_store.control import ControlState
from coppermind_store.notes import LocalStore, _move_file

from coppermind.store_protocol import MoveNote, PreconditionRequired, RenameNote, ValidationFailed


def _store(tmp_path: Path) -> LocalStore:
    notes_root = tmp_path / "notes"
    notes_root.mkdir()
    sources_root = tmp_path / "sources"
    sources_root.mkdir()
    control = ControlState(tmp_path / "state")
    control.ensure_defaults()
    # These tests return before opening a database session.
    return LocalStore(notes_root, control, None, sources_root)


@pytest.mark.parametrize("folder", ["../Work", "/Work", "Work/../../Outside"])
@pytest.mark.asyncio
async def test_move_refuses_a_folder_that_escapes_the_notes_root(
    tmp_path: Path, folder: str
) -> None:
    with pytest.raises(ValidationFailed):
        await _store(tmp_path).move_note("note-id", MoveNote(target_folder=folder), None)


@pytest.mark.parametrize("folder", ["_Sources", "_Sources/Plaud"])
@pytest.mark.asyncio
async def test_move_refuses_the_sources_tree(tmp_path: Path, folder: str) -> None:
    with pytest.raises(ValidationFailed):
        await _store(tmp_path).move_note("note-id", MoveNote(target_folder=folder), None)


@pytest.mark.asyncio
async def test_move_sanitizes_each_folder_component(tmp_path: Path) -> None:
    from coppermind.store_protocol import CreateNote
    from services.store.tests.test_move_rename_dated_and_failures import _store as mirrored

    store, mirror = mirrored(tmp_path)
    created = await store.create_note(CreateNote(title="Note", frontmatter={"type": "note"}))

    moved = await store.move_note(created.id, MoveNote(target_folder="Work?/Customer*"), None)

    assert moved.id == created.id
    assert moved.path == "Work/Customer/Note.md"
    assert (store.notes_root / "Work/Customer/Note.md").is_file()
    assert not (store.notes_root / created.path).exists()
    assert mirror.rows[created.id]["path"] == "Work/Customer/Note.md"


@pytest.mark.asyncio
async def test_rename_refuses_a_missing_if_match_before_lookup(tmp_path: Path) -> None:
    with pytest.raises(PreconditionRequired):
        await _store(tmp_path).rename_note("note-id", RenameNote(title="New title"), None)


def test_file_move_renames_bytes_without_leaving_a_copy(tmp_path: Path) -> None:
    source = tmp_path / "Review" / "Note.md"
    target = tmp_path / "Work" / "Note.md"
    source.parent.mkdir()
    source.write_bytes(b"note bytes")

    _move_file(source, target)

    assert target.read_bytes() == b"note bytes"
    assert not source.exists()


def test_file_move_does_not_replace_an_existing_target(tmp_path: Path) -> None:
    source = tmp_path / "Review" / "Note.md"
    target = tmp_path / "Work" / "Note.md"
    source.parent.mkdir()
    target.parent.mkdir()
    source.write_bytes(b"source")
    target.write_bytes(b"target")

    with pytest.raises(FileExistsError):
        _move_file(source, target)

    assert source.read_bytes() == b"source"
    assert target.read_bytes() == b"target"


@pytest.mark.asyncio
@pytest.mark.parametrize("folder", ["Archive/_Sources", "archive/_sources/Plaud"])
async def test_move_refuses_nested_configured_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder: str
) -> None:
    store = _store(tmp_path)
    settings = store.control.settings()
    settings.notes.sources_folder = "Archive/_Sources"
    monkeypatch.setattr(store.control, "settings", lambda: settings)
    with pytest.raises(ValidationFailed):
        await store.move_note("note-id", MoveNote(target_folder=folder), None)


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
@pytest.mark.parametrize("title", ["Why: this/that?", "CON", "A *question*"])
def test_rename_preserves_title_and_exact_frontmatter(ending: str, title: str) -> None:
    from coppermind_store.notes import _renamed_bytes

    prefix = ending.join(
        ["---", "# Keep this comment", 'custom: "quoted"', "tags: [one, two]", "---", ""]
    )
    original = (prefix + f"# Old{ending}{ending}Body without final newline").encode()
    data, body = _renamed_bytes(original, title)
    expected_body = f"# {title}{ending}{ending}Body without final newline"
    assert body == expected_body
    assert data == (prefix + expected_body).encode()


def test_rename_adds_a_heading_without_rewriting_frontmatter() -> None:
    from coppermind_store.notes import _renamed_bytes

    prefix = b'---\r\ncustom: "quoted" # comment\r\n---\r\n'
    data, body = _renamed_bytes(prefix + b"Body\n", "New: title?")
    assert body == "# New: title?\n\nBody\n"
    assert data == prefix + body.encode()


@pytest.mark.asyncio
async def test_rename_keeps_requested_title_in_response_and_heading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock

    from coppermind_store import notes
    from sqlalchemy.ext.asyncio import AsyncSession

    from coppermind.ids import new_id

    store = _store(tmp_path)
    note_id = new_id()
    path = store.notes_root / "Old.md"
    prefix = (
        f"---\r\nid: {note_id}\r\n# Keep me\r\ndate: 2026-09-08\r\n"
        "schema_version: 1\r\ntype: note\r\ncontext: internal\r\nreviewed: false\r\n"
        'sources: []\r\ntags: []\r\ncustom: "quoted"\r\n---\r\n'
    ).encode()
    original = prefix + b"# Old\r\n\r\nBody\r\n"
    path.write_bytes(original)
    monkeypatch.setattr(store, "_locate", AsyncMock(return_value=("Old.md", path)))
    session = AsyncMock(spec=AsyncSession)
    session.add = MagicMock()

    @asynccontextmanager
    async def transaction(_factory):
        yield session

    monkeypatch.setattr(notes, "transaction", transaction)
    renamed = await store.rename_note(
        note_id, RenameNote(title="Old?"), notes.content_hash(original)
    )
    assert renamed.id == note_id
    assert renamed.title == "Old?"
    assert renamed.path == "Old.md"
    assert renamed.body == "# Old?\r\n\r\nBody\r\n"
    assert path.read_bytes() == prefix + renamed.body.encode()


def test_file_move_refuses_a_target_created_at_the_filesystem_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coppermind_store import notes

    source = tmp_path / "Old.md"
    target = tmp_path / "New.md"
    source.write_bytes(b"Original")
    real_link = notes.os.link
    real_rename = notes.os.rename

    def raced_link(source: Path, target: Path) -> None:
        target.write_bytes(b"Device note")
        real_link(source, target)

    def raced_rename(source: Path, target: Path) -> None:
        target.write_bytes(b"Device note")
        real_rename(source, target)

    monkeypatch.setattr(notes.os, "link", raced_link)
    monkeypatch.setattr(notes.os, "rename", raced_rename)
    with pytest.raises(FileExistsError):
        _move_file(source, target)
    assert target.read_bytes() == b"Device note"
    assert source.read_bytes() == b"Original"
