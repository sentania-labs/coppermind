"""Declared field filters over a real, transactionally updated mirror."""

import json

import pytest
import sqlalchemy as sa
from coppermind_store.notes import LocalStore

from coppermind.db.models import Note
from coppermind.store_protocol import (
    CreateNote,
    FieldIndexNotReady,
    FilterCursorConflict,
    NoteQuery,
)


def query(field, op, value, **kwargs):
    return NoteQuery(filter=json.dumps({"all": [dict(field=field, op=op, value=value)]}), **kwargs)


def declare(store, name, kind):
    state = store.control.store.read("schema")
    body = dict(state.body)
    body["keys"] = [*body["keys"], dict(name=name, kind=kind)]
    store.control.store.write("schema", body, if_revision=state.revision)


async def create(store, title, **fields):
    return await store.create_note(
        CreateNote(
            title=title,
            frontmatter={
                "date": "2026-10-15",
                "type": "reference",
                "context": "internal",
                **fields,
            },
        )
    )


async def test_backfill_ready_and_live_updates(store: LocalStore):
    note = await create(store, "Existing", custom="Exact")
    declare(store, "custom", "string")
    with pytest.raises(FieldIndexNotReady):
        await store.list_notes(query("custom", "eq", "Exact"))
    await store.field_index.refresh()
    assert store.field_index.receipt()["state"] == "succeeded"
    assert [n.id for n in (await store.list_notes(query("custom", "eq", "Exact"))).items] == [
        note.id
    ]
    assert not (await store.list_notes(query("custom", "eq", "exact"))).items
    async with store.session_factory() as session, session.begin():
        await session.execute(
            sa.update(Note)
            .where(Note.id == note.id)
            .values(frontmatter={**note.frontmatter, "custom": "Changed"})
        )
    assert not (await store.list_notes(query("custom", "eq", "Exact"))).items
    assert (await store.list_notes(query("custom", "eq", "Changed"))).items
    declare(store, "another", "bool")
    assert (await store.list_notes(query("custom", "eq", "Changed"))).items
    with pytest.raises(FieldIndexNotReady):
        await store.list_notes(query("another", "eq", True))


async def test_types_invalid_values_dates_and_lists(store: LocalStore):
    for name, kind in [("count", "int"), ("enabled", "bool"), ("people", "list")]:
        declare(store, name, kind)
    note = await create(store, "Typed", count=2, enabled=True, people=["Alice", "Bob"])
    await store.field_index.refresh()
    for field, op, value in [
        ("count", "eq", 2),
        ("enabled", "eq", True),
        ("people", "contains", "Alice"),
        ("date", "gte", "2026-10-15"),
        ("date", "lte", "2026-10-15"),
    ]:
        assert (await store.list_notes(query(field, op, value))).items
    assert not (await store.list_notes(query("people", "contains", "Ali"))).items
    assert not (await store.list_notes(query("count", "eq", 3))).items
    assert not (await store.list_notes(query("count", "eq", 2, reviewed=True))).items
    async with store.session_factory() as session, session.begin():
        await session.execute(
            sa.update(Note)
            .where(Note.id == note.id)
            .values(
                frontmatter={"count": True, "enabled": "true", "date": "2026-02-30", "people": None}
            )
        )
    snapshot = await store.field_index.snapshot()
    assert snapshot["invalid_count"] == 3
    assert not (await store.list_notes(query("count", "eq", 1))).items
    assert not (await store.list_notes(query("people", "contains", "Alice"))).items


async def test_cursor_binds_filters_and_schema(store: LocalStore):
    await create(store, "One")
    await create(store, "Two")
    await store.field_index.refresh()
    first = await store.list_notes(query("type", "eq", "reference", limit=1))
    assert first.next_cursor
    second = await store.list_notes(query("type", "eq", "reference", cursor=first.next_cursor))
    assert len(second.items) == 1
    with pytest.raises(FilterCursorConflict):
        await store.list_notes(query("type", "eq", "work", cursor=first.next_cursor))
    declare(store, "later", "string")
    with pytest.raises(FilterCursorConflict):
        await store.list_notes(query("type", "eq", "reference", cursor=first.next_cursor))


async def test_search_preserves_review_gate_and_binds_cursor(store: LocalStore):
    from coppermind_store import filing, indexer

    from coppermind.store_protocol import SearchQuery

    declare(store, "people", "list")
    visible = await create(store, "Warehouse first", reviewed=True, people=["Alice"])
    await create(store, "Warehouse second", reviewed=True, people=["Alice"])
    await create(store, "Warehouse draft", people=["Alice"])
    await filing.file_reviewed(store)
    await indexer.catch_up(store)
    await store.field_index.refresh()
    custom = query("people", "contains", "Alice").filter
    page = await store.search(SearchQuery(q="warehouse", filter=custom, limit=1))
    assert len(page.items) == 1 and page.next_cursor
    second = await store.search(SearchQuery(q="warehouse", filter=custom, cursor=page.next_cursor))
    assert len(second.items) == 1
    assert visible.id in {page.items[0].id, second.items[0].id}
    with pytest.raises(FilterCursorConflict):
        await store.search(
            SearchQuery(
                q="warehouse",
                filter=query("people", "contains", "Bob").filter,
                cursor=page.next_cursor,
            )
        )


async def test_failed_rebuild_preserves_ready_generation(store: LocalStore, monkeypatch):
    from coppermind_store import field_index

    await create(store, "Survivor")
    await store.field_index.refresh()
    original = (await store.field_index.snapshot())["active_generation"]
    real = field_index.definitions
    calls = 0

    def changing_schema(current):
        nonlocal calls
        calls += 1
        keys = real(current)
        return keys if calls < 2 else []

    monkeypatch.setattr(field_index, "definitions", changing_schema)
    await store.field_index.refresh(force=True)
    assert store.field_index.receipt()["state"] == "failed"
    monkeypatch.setattr(field_index, "definitions", real)
    assert (await store.field_index.snapshot())["active_generation"] == original
    assert (await store.list_notes(query("type", "eq", "reference"))).items
    store.field_index.request_rebuild()
    await store.field_index.refresh()
    assert store.field_index.receipt()["state"] == "succeeded"


async def test_explicit_rebuild_survives_restart(store: LocalStore):
    from coppermind_store.field_index import FieldIndex

    await create(store, "Restart")
    await store.field_index.refresh()
    original = (await store.field_index.snapshot())["active_generation"]
    store.field_index.request_rebuild()
    restarted = FieldIndex(store)
    await restarted.refresh()
    assert restarted.receipt()["state"] == "succeeded"
    assert (await restarted.snapshot())["active_generation"] != original
