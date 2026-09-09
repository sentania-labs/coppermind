"""What actually reaches the notes filesystem and the database, and in what order."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from coppermind_store.fs import content_hash
from coppermind_store.notes import LocalStore

from coppermind import frontmatter as fm
from coppermind.db.models import Note
from coppermind.ids import is_valid_id
from coppermind.store_protocol import (
    CreateNote,
    MetadataUnavailable,
    NotFound,
    ValidationFailed,
)

MEETING = {
    "date": "2026-09-08",
    "type": "meeting",
    "context": "customer",
    "account": "Ameren",
    "tags": ["architecture"],
}


async def test_a_created_note_is_a_file_first_and_a_row_second(store: LocalStore, session_factory):
    note = await store.create_note(
        CreateNote(title="Ameren Architecture Sync", body="## Key points\n", frontmatter=MEETING)
    )

    assert is_valid_id(note.id)
    assert note.path == "Review/2026-09-08 Ameren Architecture Sync.md"

    on_disk = (store.notes_root / note.path).read_bytes()
    assert content_hash(on_disk) == note.content_hash

    frontmatter, body = fm.parse(on_disk.decode("utf-8"))
    assert frontmatter["id"] == note.id
    assert frontmatter["reviewed"] is False
    assert frontmatter["sources"] == []
    assert body.startswith("# Ameren Architecture Sync")

    async with session_factory() as session:
        row = (await session.execute(sa.select(Note).where(Note.id == note.id))).scalar_one()
    assert row.path == note.path
    assert row.account == "Ameren"
    assert row.reviewed is False
    assert row.content_hash == note.content_hash


async def test_a_note_reads_back_by_its_identifier(store: LocalStore):
    request = CreateNote(title="Runbook", frontmatter={"type": "reference"})
    created = await store.create_note(request)
    fetched = await store.get_note(created.id)
    assert fetched.id == created.id
    assert fetched.path == "Review/Runbook.md"
    assert fetched.content_hash == created.content_hash

    raw = await store.read_raw(created.id)
    assert raw.text == (store.notes_root / created.path).read_text(encoding="utf-8")


async def test_an_unknown_identifier_is_a_miss_not_an_error(store: LocalStore):
    with pytest.raises(NotFound):
        await store.get_note("01K4Q8Z3N7V2X9M1B5C6D8E0F2")


async def test_two_notes_with_the_same_title_both_survive(store: LocalStore):
    first = await store.create_note(CreateNote(title="Weekly sync", frontmatter=MEETING))
    second = await store.create_note(CreateNote(title="Weekly sync", frontmatter=MEETING))
    assert first.id != second.id
    assert second.path.endswith("(2).md")
    assert (store.notes_root / first.path).is_file()
    assert (store.notes_root / second.path).is_file()


async def test_a_schema_violation_writes_nothing_at_all(store: LocalStore, session_factory):
    with pytest.raises(ValidationFailed) as raised:
        await store.create_note(CreateNote(title="No account", frontmatter={"context": "customer"}))
    assert any("account" in problem for problem in raised.value.errors)
    assert list((store.notes_root).rglob("*.md")) == []
    async with session_factory() as session:
        counted = await session.execute(sa.select(sa.func.count()).select_from(Note))
    assert counted.scalar_one() == 0


async def test_a_database_outage_refuses_the_write_and_leaves_no_orphan_file(
    unreachable_store: LocalStore,
):
    """The captain's rule, made concrete.

    Writes return a clean failure while PostgreSQL is away rather than half
    succeeding. The notes filesystem is untouched, so Obsidian Sync and anyone
    editing on a device carry on unaffected.
    """
    with pytest.raises(MetadataUnavailable):
        await unreachable_store.create_note(CreateNote(title="During an outage"))
    assert list(unreachable_store.notes_root.rglob("*.md")) == []


async def test_a_read_during_an_outage_reports_the_outage(unreachable_store: LocalStore):
    with pytest.raises(MetadataUnavailable):
        await unreachable_store.get_note("01K4Q8Z3N7V2X9M1B5C6D8E0F2")


async def test_status_reports_both_halves_of_readiness(
    store: LocalStore, unreachable_store: LocalStore
):
    await store.create_note(CreateNote(title="Counted"))
    healthy = await store.status()
    assert healthy.notes_filesystem.ok is True
    assert healthy.metadata.ok is True
    assert healthy.note_count == 1

    degraded = await unreachable_store.status()
    assert degraded.notes_filesystem.ok is True
    assert degraded.metadata.ok is False
