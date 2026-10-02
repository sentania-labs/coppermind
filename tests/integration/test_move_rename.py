"""Move, rename, folder counts, and reconciliation against PostgreSQL."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from coppermind_store.notes import LocalStore
from coppermind_store.reconciler import reconcile_once

from coppermind.db.models import Note, OutboxEvent
from coppermind.store_protocol import CreateNote, MoveNote, PathCollision, RenameNote


def request(title: str) -> CreateNote:
    return CreateNote(
        title=title,
        body="Body\n",
        frontmatter={
            "date": "2026-09-08",
            "type": "note",
            "context": "internal",
            "reviewed": False,
            "sources": [],
            "tags": [],
        },
    )


async def test_move_keeps_identity_and_updates_the_mirror(
    store: LocalStore, session_factory
) -> None:
    created = await store.create_note(request("Move me"))
    moved = await store.move_note(created.id, MoveNote(target_folder="Work"), None)

    assert moved.id == created.id
    assert moved.path == "Work/Move me.md"
    assert not (store.notes_root / created.path).exists()
    assert (store.notes_root / moved.path).is_file()
    async with session_factory() as session:
        row = await session.get(Note, created.id)
        assert row is not None
        assert row.path == moved.path
        event = (await session.scalars(sa.select(OutboxEvent))).one()
        assert event.event_type == "note.moved"
        assert event.note_id == created.id
        assert event.payload == {"from_path": created.path, "path": moved.path}


async def test_rename_keeps_identity_and_renames_the_file(store: LocalStore) -> None:
    created = await store.create_note(request("Old title"))
    renamed = await store.rename_note(
        created.id, RenameNote(title="New title"), created.content_hash
    )

    assert renamed.id == created.id
    assert renamed.path == "Review/New title.md"
    assert not (store.notes_root / created.path).exists()
    assert (store.notes_root / renamed.path).is_file()


async def test_move_reports_a_live_path_collision(store: LocalStore) -> None:
    moving = await store.create_note(request("Same"))
    existing = await store.create_note(request("Same"))
    await store.move_note(existing.id, MoveNote(target_folder="Work"), None)

    with pytest.raises(PathCollision):
        await store.move_note(moving.id, MoveNote(target_folder="Work"), None)


async def test_reconcile_after_move_keeps_one_identity(store: LocalStore, session_factory) -> None:
    created = await store.create_note(request("Reconcile me"))
    moved = await store.move_note(created.id, MoveNote(target_folder="Filed"), None)

    await reconcile_once(store, full=True)

    async with session_factory() as session:
        rows = list((await session.scalars(sa.select(Note))).all())
    assert [(row.id, row.path, row.state) for row in rows] == [(created.id, moved.path, "ok")]


async def test_folder_tree_counts_notes_in_descendants(store: LocalStore) -> None:
    first = await store.create_note(request("One"))
    second = await store.create_note(request("Two"))
    await store.move_note(first.id, MoveNote(target_folder="Work/Customers"), None)
    await store.move_note(second.id, MoveNote(target_folder="Work"), None)

    tree = await store.list_folders()
    work = next(item for item in tree.children if item.path == "Work")
    customers = next(item for item in work.children if item.path == "Work/Customers")
    assert work.note_count == 2
    assert customers.note_count == 1


async def test_move_refuses_an_unmirrored_destination(store: LocalStore) -> None:
    created = await store.create_note(request("Same"))
    target = store.notes_root / "Work/Same.md"
    target.parent.mkdir()
    target.write_bytes(b"Unmirrored device note")
    original = (store.notes_root / created.path).read_bytes()
    with pytest.raises(PathCollision):
        await store.move_note(created.id, MoveNote(target_folder="Work"), None)
    assert target.read_bytes() == b"Unmirrored device note"
    assert (store.notes_root / created.path).read_bytes() == original
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize("operation", ["move", "rename"])
async def test_destination_created_during_move_is_never_clobbered(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from pathlib import Path

    from coppermind_store import notes

    created = await store.create_note(request("Original"))
    target = store.notes_root / ("Work/Original.md" if operation == "move" else "Review/Renamed.md")
    real_link = notes.os.link
    real_rename = notes.os.rename

    def raced_link(source: Path, destination: Path) -> None:
        destination.write_bytes(b"Device wins")
        real_link(source, destination)

    def raced_rename(source: Path, destination: Path) -> None:
        destination.write_bytes(b"Device wins")
        real_rename(source, destination)

    monkeypatch.setattr(notes.os, "link", raced_link)
    monkeypatch.setattr(notes.os, "rename", raced_rename)
    with pytest.raises(PathCollision):
        if operation == "move":
            await store.move_note(created.id, MoveNote(target_folder="Work"), None)
        else:
            await store.rename_note(created.id, RenameNote(title="Renamed"), created.content_hash)
    assert target.read_bytes() == b"Device wins"
    assert (store.notes_root / created.path).exists()


async def test_rename_refuses_case_insensitive_stem_collision(store: LocalStore) -> None:
    created = await store.create_note(request("Original"))
    target = store.notes_root / "Review/foo.md"
    target.write_bytes(b"Device note")
    original = (store.notes_root / created.path).read_bytes()
    with pytest.raises(PathCollision):
        await store.rename_note(created.id, RenameNote(title="FOO"), created.content_hash)
    assert target.read_bytes() == b"Device note"
    assert (store.notes_root / created.path).read_bytes() == original


@pytest.mark.parametrize("conditional", [True, False])
async def test_move_rechecks_bytes_after_collision_query(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch, conditional: bool
) -> None:
    from coppermind_store import notes
    from sqlalchemy.ext.asyncio import AsyncSession

    from coppermind.store_protocol import VersionConflict

    created = await store.create_note(request("Original"))
    path = store.notes_root / created.path
    changed = path.read_bytes() + b"Device edit\n"
    original_execute = AsyncSession.execute

    async def execute_with_edit(self, statement, *args, **kwargs):
        result = await original_execute(self, statement, *args, **kwargs)
        if str(statement).startswith("SELECT notes.id"):
            path.write_bytes(changed)
        return result

    monkeypatch.setattr(AsyncSession, "execute", execute_with_edit)
    if conditional:
        with pytest.raises(VersionConflict):
            await store.move_note(created.id, MoveNote(target_folder="Work"), created.content_hash)
        assert path.read_bytes() == changed
        assert not (store.notes_root / "Work/Original.md").exists()
    else:
        moved = await store.move_note(created.id, MoveNote(target_folder="Work"), None)
        assert moved.content_hash == notes.content_hash(changed)
        assert moved.body.endswith("Device edit\n")
        assert (store.notes_root / moved.path).read_bytes() == changed
