"""PostgreSQL backed listing over current note files."""

from __future__ import annotations

from datetime import date

import pytest
from coppermind_store import notes as notes_module
from coppermind_store.notes import LocalStore
from coppermind_store.reconciler import reconcile_once

from coppermind.store_protocol import (
    CreateNote,
    MetadataUnavailable,
    NoteQuery,
    ValidationFailed,
)


async def _create(
    store: LocalStore,
    title: str,
    *,
    note_date: str,
    note_type: str,
    context: str,
    account: str | None = None,
    reviewed: bool = False,
    tags: list[str] | None = None,
):
    frontmatter: dict[str, object] = {
        "date": note_date,
        "type": note_type,
        "context": context,
        "reviewed": reviewed,
        "tags": tags or [],
    }
    if account is not None:
        frontmatter["account"] = account
    return await store.create_note(CreateNote(title=title, frontmatter=frontmatter))


async def test_filters_use_the_current_files_for_every_known_note(store: LocalStore):
    ameren = await _create(
        store,
        "Ameren Architecture",
        note_date="2026-09-01",
        note_type="meeting",
        context="customer",
        account="Ameren",
        tags=["architecture", "vcf"],
    )
    runbook = await _create(
        store,
        "Database Runbook",
        note_date="2026-09-10",
        note_type="note",
        context="internal",
        reviewed=True,
        tags=["runbook"],
    )
    reference = await _create(
        store,
        "Vendor Reference",
        note_date="2026-09-20",
        note_type="reference",
        context="external",
        tags=["architecture"],
    )

    ameren_path = store.notes_root / ameren.path
    edited = (
        ameren_path.read_text(encoding="utf-8")
        .replace("reviewed: false", "reviewed: true", 1)
        .replace("# Ameren Architecture", "# Current Ameren Architecture", 1)
    )
    ameren_path.write_text(edited, encoding="utf-8")
    await reconcile_once(store)

    async def ids(**filters: object) -> set[str]:
        page = await store.list_notes(NoteQuery.model_validate(filters))
        return {item.id for item in page.items}

    assert await ids(folder="Review") == {ameren.id, runbook.id, reference.id}
    assert await ids(reviewed=True) == {ameren.id, runbook.id}
    assert await ids(type="meeting") == {ameren.id}
    assert await ids(context="external") == {reference.id}
    assert await ids(account="Ameren") == {ameren.id}
    assert await ids(**{"from": date(2026, 9, 5), "to": date(2026, 9, 15)}) == {runbook.id}
    assert await ids(tag="architecture") == {ameren.id, reference.id}
    assert await ids(state="ok") == {ameren.id, runbook.id, reference.id}

    current = (await store.list_notes(NoteQuery(type="meeting"))).items[0]
    assert current.title == "Current Ameren Architecture"
    assert current.content_hash != ameren.content_hash


async def test_state_filters_observe_missing_and_unparseable_known_paths(store: LocalStore):
    missing = await _create(
        store,
        "Removed Note",
        note_date="2026-09-01",
        note_type="note",
        context="internal",
    )
    broken = await _create(
        store,
        "Broken Note",
        note_date="2026-09-02",
        note_type="note",
        context="internal",
    )
    (store.notes_root / missing.path).unlink()
    broken_path = store.notes_root / broken.path
    broken_path.write_text(
        broken_path.read_text(encoding="utf-8").replace("tags: []", "tags: ["),
        encoding="utf-8",
    )
    await reconcile_once(store)

    missing_page = await store.list_notes(NoteQuery(state="missing"))
    broken_page = await store.list_notes(NoteQuery(state="unparsed"))
    assert [item.id for item in missing_page.items] == [missing.id]
    assert [item.id for item in broken_page.items] == [broken.id]
    assert [item.state for item in missing_page.items] == ["missing"]
    assert [item.state for item in broken_page.items] == ["unparsed"]


async def test_a_note_removed_on_a_device_lists_as_missing_beside_a_present_one(
    store: LocalStore,
):
    """An unfiltered listing says which summaries came from a file and which did not."""
    removed = await _create(
        store,
        "Removed Note",
        note_date="2026-09-01",
        note_type="note",
        context="internal",
    )
    present = await _create(
        store,
        "Surviving Note",
        note_date="2026-09-02",
        note_type="note",
        context="internal",
    )
    (store.notes_root / removed.path).unlink()
    await reconcile_once(store)

    page = await store.list_notes(NoteQuery())

    assert {item.id: item.state for item in page.items} == {
        removed.id: "missing",
        present.id: "ok",
    }


async def test_the_folder_filter_takes_the_folder_exactly_as_written(store: LocalStore):
    note = await _create(
        store,
        "Filed Note",
        note_date="2026-09-01",
        note_type="note",
        context="internal",
    )
    assert note.path.startswith("Review/")

    async def ids(folder: str) -> set[str]:
        page = await store.list_notes(NoteQuery(folder=folder))
        return {item.id for item in page.items}

    assert await ids("Review") == {note.id}
    assert await ids("/Review") == set()
    assert await ids("Review/") == set()


async def test_cursor_pages_four_notes_two_at_a_time_without_gaps(store: LocalStore):
    created = [
        await _create(
            store,
            title,
            note_date=f"2026-09-0{number}",
            note_type="note",
            context="internal",
        )
        for number, title in enumerate(("Alpha", "Bravo", "Charlie", "Delta"), start=1)
    ]

    first = await store.list_notes(NoteQuery(limit=2))
    assert len(first.items) == 2
    assert first.next_cursor is not None
    second = await store.list_notes(NoteQuery(limit=2, cursor=first.next_cursor))
    assert len(second.items) == 2
    assert second.next_cursor is None

    returned = [item.id for item in [*first.items, *second.items]]
    expected = [note.id for note in sorted(created, key=lambda note: note.id)]
    assert returned == expected
    assert len(returned) == len(set(returned))


async def test_a_page_reads_the_reconciled_mirror_not_note_files(store: LocalStore, monkeypatch):
    for number, title in enumerate(("Alpha", "Bravo", "Charlie", "Delta", "Echo"), start=1):
        await _create(
            store,
            title,
            note_date=f"2026-09-0{number}",
            note_type="note",
            context="internal",
        )

    def refused(*_args, **_kwargs):
        raise AssertionError("listing opened a note file")

    monkeypatch.setattr(notes_module, "_read", refused)
    page = await store.list_notes(NoteQuery(limit=2))

    assert len(page.items) == 2
    assert page.next_cursor is not None


async def test_rename_between_pages_does_not_change_where_the_cursor_resumes(
    store: LocalStore,
):
    created = [
        await _create(
            store,
            title,
            note_date=f"2026-09-0{number}",
            note_type="note",
            context="internal",
        )
        for number, title in enumerate(("Alpha", "Bravo", "Charlie", "Delta"), start=1)
    ]
    first = await store.list_notes(NoteQuery(limit=2))
    assert first.next_cursor is not None

    first_ids = {item.id for item in first.items}
    remaining = next(note for note in created if note.id not in first_ids)
    old_path = store.notes_root / remaining.path
    moved_path = store.notes_root / "Filed" / "Renamed.md"
    moved_path.parent.mkdir()
    old_path.rename(moved_path)
    await reconcile_once(store)

    second = await store.list_notes(NoteQuery(limit=2, cursor=first.next_cursor))
    returned = [item.id for item in [*first.items, *second.items]]
    assert returned == sorted(note.id for note in created)
    assert len(returned) == len(set(returned))


async def test_invalid_cursor_is_a_validation_failure(store: LocalStore):
    with pytest.raises(ValidationFailed, match="cursor"):
        await store.list_notes(NoteQuery(cursor="not-a-cursor"))


async def test_listing_refuses_when_metadata_is_unavailable(unreachable_store: LocalStore):
    with pytest.raises(MetadataUnavailable):
        await unreachable_store.list_notes(NoteQuery())
