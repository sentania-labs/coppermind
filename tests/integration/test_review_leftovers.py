"""A review copy left beside a note that was already filed."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from coppermind_store.fs import content_hash
from coppermind_store.notes import LocalStore
from coppermind_store.reconciler import note_collisions, reconcile_once
from structlog.testing import capture_logs

from coppermind.db.models import Note
from coppermind.store_protocol import CreateNote

FILED = "Notes/2026/2026-11/Monthly TAM call.md"
LEFTOVER = "Review/Monthly TAM call.md"


async def _filed_note(store: LocalStore, *, filed: bool = True) -> tuple[str, bytes]:
    """A note filed out of Review, with the bytes it had when it was filed."""
    note = await store.create_note(CreateNote(title="TAM call", frontmatter={"type": "reference"}))
    await reconcile_once(store)
    original = (store.notes_root / note.path).read_bytes()
    target = store.notes_root / FILED
    target.parent.mkdir(parents=True)
    (store.notes_root / note.path).rename(target)
    await reconcile_once(store)
    await _mark_filed(store, note.id, filed)
    return note.id, original


async def _mark_filed(store: LocalStore, note_id: str, filed: bool) -> None:
    async with store.session_factory() as session:
        await session.execute(sa.update(Note).where(Note.id == note_id).values(filed=filed))
        await session.commit()


def _leftover(store: LocalStore, data: bytes) -> None:
    path = store.notes_root / LEFTOVER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _warnings(logged: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [e for e in logged if e["event"] == "duplicate note identity left unresolved"]


async def test_an_identical_review_copy_of_a_filed_note_is_removed(store: LocalStore):
    note_id, data = await _filed_note(store)
    _leftover(store, data)

    with capture_logs() as logged:
        counts = await reconcile_once(store)

    assert not (store.notes_root / LEFTOVER).exists()
    assert (store.notes_root / FILED).read_bytes() == data
    assert counts["duplicates"] == 0
    assert _warnings(logged) == []
    assert await note_collisions(store, note_id) == []


async def test_a_review_copy_matching_a_recorded_snapshot_is_removed(store: LocalStore):
    note_id, data = await _filed_note(store)
    filed = store.notes_root / FILED
    filed.write_bytes(data + b"\nEdited after filing.\n")
    await reconcile_once(store)
    await _mark_filed(store, note_id, True)
    journal = store.control.store.state_dir / "filing" / f"{note_id}.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({"copies": [{"path": "x", "hash": content_hash(data)}]}))
    _leftover(store, data)

    await reconcile_once(store)

    assert not (store.notes_root / LEFTOVER).exists()
    assert filed.read_bytes().endswith(b"Edited after filing.\n")


async def test_a_differing_review_copy_is_kept_and_put_on_the_note(store: LocalStore):
    note_id, data = await _filed_note(store)
    edited = data + b"\nWritten on the phone.\n"
    _leftover(store, edited)

    with capture_logs() as first:
        counts = await reconcile_once(store)
    with capture_logs() as second:
        await reconcile_once(store)

    assert (store.notes_root / LEFTOVER).read_bytes() == edited
    assert (store.notes_root / FILED).read_bytes() == data
    assert counts["duplicates"] == 1
    assert len(_warnings(first)) == 1
    assert set(_warnings(first)[0]["paths"]) == {FILED, LEFTOVER}
    assert _warnings(second) == []
    reasons = " ".join(await note_collisions(store, note_id))
    assert LEFTOVER in reasons and FILED in reasons and "Compare them" in reasons


async def test_a_collision_is_logged_again_when_the_files_involved_change(store: LocalStore):
    _, data = await _filed_note(store)
    _leftover(store, data + b"one\n")
    await reconcile_once(store)
    other = store.notes_root / "Review" / "Second.md"
    other.write_bytes(data + b"two\n")

    with capture_logs() as logged:
        await reconcile_once(store)

    assert len(_warnings(logged)) == 1


async def test_a_review_copy_of_a_note_not_yet_filed_is_never_removed(store: LocalStore):
    _, data = await _filed_note(store, filed=False)
    _leftover(store, data)

    counts = await reconcile_once(store)

    assert (store.notes_root / LEFTOVER).read_bytes() == data
    assert counts["duplicates"] == 1
