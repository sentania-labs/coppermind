"""Read-side reconciliation of known identities from the notes filesystem."""

from __future__ import annotations

import sqlalchemy as sa
from coppermind_store.notes import LocalStore

from coppermind.db.models import Note
from coppermind.store_protocol import CreateNote, NoteQuery, NoteUnparseable, NotFound


async def _row(store: LocalStore, note_id: str) -> Note:
    async with store.session_factory() as session:
        return (await session.scalars(sa.select(Note).where(Note.id == note_id))).one()


async def test_device_edit_refreshes_the_mirror_and_reads_back_by_id(store: LocalStore):
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    path = store.notes_root / note.path
    path.write_bytes(
        path.read_bytes()
        .replace(b"reviewed: false", b"reviewed: true")
        .replace(b"# Runbook", b"# Current Runbook")
    )

    counts = await store.reconcile()
    fetched = await store.get_note(note.id)
    row = await _row(store, note.id)

    assert counts["changed"] == 1
    assert fetched.title == "Current Runbook"
    assert fetched.frontmatter["reviewed"] is True
    assert row.title == fetched.title
    assert row.content_hash == fetched.content_hash
    assert row.reviewed is True


async def test_device_move_and_rename_follow_the_identity(store: LocalStore):
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    moved = store.notes_root / "Work" / "Operations Runbook.md"
    moved.parent.mkdir()
    (store.notes_root / note.path).rename(moved)

    counts = await store.reconcile()
    fetched = await store.get_note(note.id)

    assert counts["moved"] == 1
    assert fetched.id == note.id
    assert fetched.path == "Work/Operations Runbook.md"
    assert (await _row(store, note.id)).path == fetched.path


async def test_device_delete_is_missing_instead_of_present(store: LocalStore):
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    (store.notes_root / note.path).unlink()

    counts = await store.reconcile()
    page = await store.list_notes(NoteQuery())

    assert counts["missing"] == 1
    assert [(item.id, item.state, item.path) for item in page.items] == [
        (note.id, "missing", note.path)
    ]
    try:
        await store.get_note(note.id)
    except NotFound as error:
        assert error.note_id == note.id
    else:
        raise AssertionError("deleted note still read as present")


async def test_stale_path_does_not_name_the_wrong_broken_note(store: LocalStore):
    stale = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    moved = await store.create_note(CreateNote(title="Meeting", frontmatter={"type": "reference"}))
    stale_path = store.notes_root / stale.path
    moved_path = store.notes_root / moved.path
    stale_path.unlink()
    broken = moved_path.read_text(encoding="utf-8").replace("tags: []", "tags: [")
    moved_path.write_text(broken, encoding="utf-8")
    moved_path.rename(stale_path)

    await store.reconcile()

    assert (await _row(store, stale.id)).state == "missing"
    moved_row = await _row(store, moved.id)
    assert moved_row.path == stale.path
    assert moved_row.state == "unparsed"
    try:
        await store.get_note(stale.id)
    except NotFound as error:
        assert error.note_id == stale.id
    else:
        raise AssertionError("stale identity was reported as the broken note")
    try:
        await store.get_note(moved.id)
    except NoteUnparseable as error:
        assert error.note_id == moved.id
    else:
        raise AssertionError("broken note did not report its own identity")


async def test_unknown_device_created_file_is_left_unchanged(store: LocalStore):
    unknown = store.notes_root / "Review" / "Made on phone.md"
    unknown.parent.mkdir(parents=True, exist_ok=True)
    original = b"# Made on phone\n\nNo identity yet.\n"
    unknown.write_bytes(original)

    counts = await store.reconcile()

    async with store.session_factory() as session:
        assert (await session.scalar(sa.select(sa.func.count()).select_from(Note))) == 0
    assert unknown.read_bytes() == original
    assert counts == {"changed": 0, "moved": 0, "missing": 0, "unparsed": 0}
