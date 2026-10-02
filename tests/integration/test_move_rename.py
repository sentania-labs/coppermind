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
