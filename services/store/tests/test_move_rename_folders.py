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
    return LocalStore(notes_root, control, None, sources_root)  # type: ignore[arg-type]


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
    store = _store(tmp_path)
    with pytest.raises(Exception) as raised:
        await store.move_note("note-id", MoveNote(target_folder="Work?/Customer*"), None)
    assert not isinstance(raised.value, ValidationFailed)


@pytest.mark.asyncio
async def test_rename_refuses_a_missing_if_match_before_lookup(tmp_path: Path) -> None:
    with pytest.raises(PreconditionRequired):
        await _store(tmp_path).rename_note("note-id", RenameNote(title="New title"), None)  # type: ignore[arg-type]


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
