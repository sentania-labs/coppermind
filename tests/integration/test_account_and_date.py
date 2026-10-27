"""#59 and #60: `account` is a list, and `date` is the one date and start time field."""

import time as clock
from datetime import date

import pytest
import sqlalchemy as sa
from coppermind_store import filing, indexer, note_lifecycle
from coppermind_store.reconciler import reconcile_once

from coppermind.db.models import Note
from coppermind.ids import new_id
from coppermind.store_protocol import (
    CreateNote,
    NoteQuery,
    SearchQuery,
    ValidationFailed,
)


def hand_written(store, name: str, front: str, body: str = "Unique accountpalabra text.\n") -> str:
    note_id = new_id()
    folder = store.notes_root / "Review"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(
        f"---\nid: {note_id}\nschema_version: 1\nsources: []\n{front}---\n# {name}\n\n{body}",
        encoding="utf-8",
    )
    return note_id


async def settle(store, monkeypatch) -> None:
    """Reconcile, file every reviewed note and index it, so search can see them."""
    await reconcile_once(store)
    stable = clock.monotonic() + 31
    monkeypatch.setattr(note_lifecycle.clock, "monotonic", lambda: stable)
    await filing.file_reviewed(store)
    await reconcile_once(store)
    await indexer.catch_up(store)


async def listed(store, **filters) -> set[str]:
    return {item.id for item in (await store.list_notes(NoteQuery.model_validate(filters))).items}


async def found(store, **filters) -> set[str]:
    query = SearchQuery.model_validate({"q": "accountpalabra", **filters})
    return {hit.id for hit in (await store.search(query)).items}


async def test_a_string_and_a_one_item_list_file_list_and_filter_identically(store, monkeypatch):
    plain = hand_written(
        store,
        "Plain",
        "type: reference\ncontext: external\nreviewed: true\naccount: Xcel\ndate: 2026-10-20\n",
    )
    listy = hand_written(
        store,
        "Listy",
        "type: reference\ncontext: external\nreviewed: true\naccount: [Xcel]\ndate: 2026-10-20\n",
    )
    await settle(store, monkeypatch)
    for note_id in (plain, listy):
        note = await store.get_note(note_id)
        assert schema_clean(store, note)
    summaries = {item.id: item for item in (await store.list_notes(NoteQuery())).items}
    assert summaries[plain].account == summaries[listy].account == ["Xcel"]
    assert await listed(store, account="Xcel") == {plain, listy}
    assert await listed(store, account="United") == set()
    assert await found(store, account="Xcel") == {plain, listy}
    await store.field_index.refresh()
    flt = '{"all": [{"field": "account", "op": "contains", "value": "Xcel"}]}'
    assert await listed(store, filter=flt) == {plain, listy}


def schema_clean(store, note) -> bool:
    return store.control.schema().validate_frontmatter(dict(note.frontmatter)) == []


async def test_a_multi_account_note_is_found_by_each_entry_in_listing_and_search(
    store, monkeypatch
):
    several = hand_written(
        store,
        "Reference",
        "type: reference\ncontext: internal\nreviewed: true\n"
        "account: [Xcel, United]\ndate: 2026-10-20\n",
    )
    other = hand_written(
        store,
        "Other",
        "type: reference\ncontext: external\nreviewed: true\naccount: [Acme]\ndate: 2026-10-20\n",
    )
    none = hand_written(store, "None", "type: reference\ncontext: internal\ndate: 2026-10-20\n")
    await settle(store, monkeypatch)
    for name in ("Xcel", "United"):
        assert await listed(store, account=name) == {several}
        assert await found(store, account=name) == {several}
    assert await listed(store, account="Acme") == {other}
    async with store.session_factory() as session:
        rows = {
            row.id: list(row.account) for row in (await session.execute(sa.select(Note))).scalars()
        }
    assert rows == {several: ["Xcel", "United"], other: ["Acme"], none: []}
    await store.field_index.refresh()
    flt = '{"all": [{"field": "account", "op": "contains", "value": "United"}]}'
    assert await listed(store, filter=flt) == {several}


async def test_external_with_an_empty_account_fails_with_the_missing_account_message(store):
    messages = []
    for account in (None, [], "", [""]):
        front: dict[str, object] = {
            "type": "reference",
            "context": "external",
            "date": "2026-10-20",
        }
        if account is not None:
            front["account"] = account
        with pytest.raises(ValidationFailed) as raised:
            await store.create_note(CreateNote(title="No account", frontmatter=front))
        messages.append(raised.value.errors)
    assert messages[0] == ["account: required when context is one of external"]
    assert all(problems == messages[0] for problems in messages)
    for account in ("Xcel", ["Xcel"], ["Xcel", "United"]):
        await store.create_note(
            CreateNote(
                title=f"Has {account}",
                frontmatter={"type": "reference", "context": "external", "account": account},
            )
        )


async def test_date_only_datetime_and_legacy_time_file_to_the_same_month_folder(store, monkeypatch):
    front = "type: work\ncontext: internal\nreviewed: true\n"
    ids = [
        hand_written(store, "Day only", front + "date: 2026-10-20\ntime_unknown: true\n"),
        hand_written(store, "Datetime", front + "date: 2026-10-20T12:00:00\n"),
        hand_written(store, "Legacy", front + "date: 2026-10-20\ntime: '09:30'\n"),
    ]
    await reconcile_once(store)
    for note_id in ids:
        await store.get_note(note_id)
    stable = clock.monotonic() + 31
    monkeypatch.setattr(note_lifecycle.clock, "monotonic", lambda: stable)
    assert await filing.file_reviewed(store) == {"filed": 3, "filing_failed": 0}
    assert await store.get_problems() == []
    for note_id in ids:
        assert (await store.get_note(note_id)).path.startswith("2026-10/")
    legacy = await store.get_note(ids[2])
    raw = (store.notes_root / legacy.path).read_text(encoding="utf-8")
    assert "time: '09:30'" in raw or 'time: "09:30"' in raw or "time: 09:30" in raw


async def test_date_and_datetime_both_list_under_the_same_day_and_mirror_backfills(
    store, monkeypatch
):
    day = hand_written(
        store, "Day", "type: reference\ncontext: internal\nreviewed: true\ndate: 2026-10-20\n"
    )
    stamp = hand_written(
        store,
        "Stamp",
        "type: reference\ncontext: internal\nreviewed: true\ndate: 2026-10-20T12:00:00\n",
    )
    spaced = hand_written(
        store,
        "Spaced",
        "type: reference\ncontext: internal\nreviewed: true\ndate: '2026-10-20 12:00:00'\n",
    )
    await settle(store, monkeypatch)
    window = {"from": "2026-10-20", "to": "2026-10-20"}
    assert await listed(store, **window) == {day, stamp, spaced}
    assert await found(store, **window) == {day, stamp, spaced}
    await store.field_index.refresh()
    flt = '{"all": [{"field": "date", "op": "eq", "value": "2026-10-20"}]}'
    assert await listed(store, filter=flt) == {day, stamp, spaced}

    # A metadata rebuild backfills a mirror row that lost its date.
    async with store.session_factory() as session:
        await session.execute(sa.update(Note).values(date=None))
        await session.commit()
    assert await listed(store, **window) == set()
    await reconcile_once(store, full=True)
    assert await listed(store, **window) == {day, stamp, spaced}
    async with store.session_factory() as session:
        dates = set((await session.execute(sa.select(Note.date))).scalars())
    assert dates == {date(2026, 10, 20)}


async def test_a_stuck_review_note_with_an_unquoted_datetime_date_files_on_the_next_pass(
    store, monkeypatch
):
    stuck = hand_written(
        store,
        "Stuck",
        "type: work\ncontext: external\nreviewed: true\naccount: Example Co\n"
        "date: 2026-10-20T12:00:00\n",
    )
    await reconcile_once(store)
    await store.get_note(stuck)
    stable = clock.monotonic() + 31
    monkeypatch.setattr(note_lifecycle.clock, "monotonic", lambda: stable)
    assert await filing.file_reviewed(store) == {"filed": 1, "filing_failed": 0}
    filed = await store.get_note(stuck)
    assert filed.path.startswith("2026-10/")
    raw = (store.notes_root / filed.path).read_text(encoding="utf-8")
    assert "date: 2026-10-20T12:00:00\n" in raw
    assert "time:" not in raw and "timezone" not in raw


async def test_a_date_with_a_utc_offset_is_rejected_with_a_clear_problems_message(
    store, monkeypatch
):
    offset = hand_written(
        store,
        "Offset",
        "type: work\ncontext: internal\nreviewed: true\ndate: 2026-10-20T12:00:00\n",
    )
    await reconcile_once(store)
    path = next(store.notes_root.rglob("Offset.md"))
    path.write_text(path.read_text().replace("12:00:00", "12:00:00-05:00"), encoding="utf-8")
    await reconcile_once(store, full=True)
    await store.get_note(offset)
    stable = clock.monotonic() + 31
    monkeypatch.setattr(note_lifecycle.clock, "monotonic", lambda: stable)
    assert await filing.file_reviewed(store) == {"filed": 0, "filing_failed": 1}
    problems = await store.get_problems()
    assert any(
        p.kind == "filing"
        and p.note_id == offset
        and "date must be a local date or date-and-time without a UTC offset" in p.reason
        for p in problems
    )

    # Every write path refuses it up front with the same words.
    with pytest.raises(ValidationFailed, match="without a UTC offset"):
        await store.create_note(
            CreateNote(title="Refused", frontmatter={"date": "2026-10-20T12:00:00Z"})
        )
