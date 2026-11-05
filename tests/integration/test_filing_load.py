"""Filing under sustained reads and removal at the filesystem/mirror seam."""

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from coppermind_store import filing, notes
from coppermind_store.reconciler import reconcile_once

from coppermind.store_protocol import CreateNote, NoteQuery, NoteState, PatchFrontmatter
from tests.integration.test_note_recovery import commit, git
from tests.integration.test_search_index import ingest_request
from tests.integration.test_store_write_protocol import authorized_api


async def test_500_reviewed_notes_remain_readable_while_filing(store, monkeypatch):
    for i in range(500):
        await store.create_note(
            CreateNote(
                title=f"Load note {i}",
                body=f"Body {i}",
                frontmatter={
                    "date": (date(2002, 1, 15) + timedelta(days=i * 17)).isoformat(),
                    "type": "reference",
                    "reviewed": True,
                },
            )
        )
    assert (await store.get_status()).counters.filing_queue_depth == 500
    moved = asyncio.Event()
    original = notes._move_file

    def move(*args):
        original(*args)
        moved.set()

    monkeypatch.setattr(notes, "_move_file", move)
    task = asyncio.create_task(filing.file_reviewed(store))
    await asyncio.wait_for(moved.wait(), timeout=60)
    reads = 0
    app, headers = authorized_api(store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api", headers=headers
    ) as client:
        while True:
            cursor = None
            while True:
                params = {"state": "ok", "limit": "100"}
                if cursor:
                    params["cursor"] = cursor
                response = await client.get("/v1/notes", params=params)
                assert response.status_code == 200, response.text
                page = response.json()
                for item in page["items"]:
                    document = await client.get(f"/v1/notes/{item['id']}")
                    assert document.status_code == 200, document.text
                    assert document.json()["id"] == item["id"]
                    reads += 1
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            if task.done():
                break
        response = await client.get("/v1/status")
        assert response.status_code == 200
        assert response.json()["counters"]["filing_queue_depth"] == 0
        assert response.json()["counters"]["filing_failures"] == 0
    assert await task == {"filed": 500, "filing_failed": 0}
    assert reads >= 500
    status = await store.get_status()
    assert status.counters.filing_queue_depth == 0
    assert status.counters.filing_failures == 0


async def test_identity_follows_move_before_mirror(store):
    note = await store.create_note(CreateNote(title="Moved", frontmatter={"type": "reference"}))
    destination = store.notes_root / "elsewhere" / Path(note.path).name
    destination.parent.mkdir()
    (store.notes_root / note.path).rename(destination)
    assert (await store.get_note(note.id)).path == destination.relative_to(
        store.notes_root
    ).as_posix()
    assert [n.id for n in (await store.list_notes(NoteQuery(state="ok"))).items] == [note.id]
    await reconcile_once(store)
    assert not [p for p in await store.get_problems() if p.kind == "path_notice"]
    assert (await store.get_status()).counters.active_problems == 0


async def test_path_notice_lasts_only_while_the_disagreement_is_open(store):
    kept = await store.create_note(CreateNote(title="Kept", frontmatter={"type": "reference"}))
    moved = await store.create_note(CreateNote(title="Moved", frontmatter={"type": "reference"}))
    away = store.notes_root / "elsewhere" / Path(kept.path).name
    away.parent.mkdir()
    (store.notes_root / kept.path).rename(away)
    (store.notes_root / moved.path).rename(store.notes_root / kept.path)
    assert (await store.get_note(moved.id)).path == kept.path

    open_notices = [p for p in await store.get_problems() if p.kind == "path_notice"]
    assert [p.note_id for p in open_notices] == [moved.id]
    assert (await store.get_status()).counters.active_problems == 1
    notice = store.control.store.state_dir / "path-notices" / f"{moved.id}.json"
    written = notice.stat().st_mtime_ns
    await store.get_note(moved.id)
    await store.get_note(moved.id)
    assert notice.stat().st_mtime_ns == written
    await reconcile_once(store)
    assert not notice.exists()
    assert not [p for p in await store.get_problems() if p.kind == "path_notice"]
    assert (await store.get_status()).counters.active_problems == 0


async def test_failure_count_drains_and_error_is_visible_by_id(store, monkeypatch):
    note = await store.create_note(
        CreateNote(
            title="Retry", frontmatter={"date": "2026-09-01", "type": "reference", "reviewed": True}
        )
    )
    original = store.move_note

    async def fail(*args):
        raise ValueError("filing destination is occupied")

    monkeypatch.setattr(store, "move_note", fail)
    assert (await filing.file_reviewed(store))["filing_failed"] == 1
    current = await store.get_note(note.id)
    assert "occupied" in current.filing_error
    assert current.filing_attempts == 1
    assert not current.filed
    assert (await store.get_status()).counters.filing_failures == 1
    monkeypatch.setattr(store, "move_note", original)
    await filing.file_reviewed(store)
    current = await store.get_note(note.id)
    assert current.filed
    assert current.filing_error is None
    assert current.filing_attempts == 2
    assert (await store.get_status()).counters.filing_failures == 0


@pytest.mark.parametrize("committed", [True, False])
async def test_removal_in_filing_has_receipt_or_explained_missing(store, monkeypatch, committed):
    git(store.notes_root, "init", "-q")
    result = await store.ingest(ingest_request())
    note = await store.get_note(result.note.id)
    await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"reviewed": True}), note.content_hash
    )
    commit(store.notes_root)
    original = store.move_note

    async def remove_after_move(*args):
        moved = await original(*args)
        (store.notes_root / moved.path).unlink()
        if committed:
            commit(store.notes_root)
        return moved

    monkeypatch.setattr(store, "move_note", remove_after_move)
    await filing.file_reviewed(store)
    await reconcile_once(store)
    state: NoteState = "deleted" if committed else "missing"
    row = (await store.list_notes(NoteQuery(state=state))).items[0]
    assert row.id == note.id
    if committed:
        recovery = await store.preview_note_restore(note.id)
        assert recovery.content_hash == row.content_hash
        assert note.id in recovery.content
    else:
        assert "vanished between filing paths" in row.state_reason
        assert row.content_hash in row.state_reason
        assert any(p.kind == "missing" and p.note_id == row.id for p in await store.get_problems())
        assert (await reconcile_once(store))["missing"] == 0
        assert (await store.list_notes(NoteQuery(state=state))).items[0].state_reason == (
            row.state_reason
        )
    assert (await store.get_status()).counters.filing_failures == 0


async def test_settled_note_deleted_before_git_is_missing_without_a_filing_claim(store):
    note = await store.create_note(
        CreateNote(
            title="Settled",
            frontmatter={"date": "2026-09-01", "type": "reference", "reviewed": True},
        )
    )
    assert (await filing.file_reviewed(store))["filed"] == 1
    assert (await filing.file_reviewed(store))["filed"] == 0
    (store.notes_root / (await store.get_note(note.id)).path).unlink()
    assert (await reconcile_once(store))["missing"] == 1
    row = (await store.list_notes(NoteQuery(state="missing"))).items[0]
    assert row.id == note.id
    assert "between filing paths" not in row.state_reason
    assert (await reconcile_once(store))["missing"] == 0


async def test_listing_stale_rows_walks_the_tree_once(store, monkeypatch):
    ids = [
        (
            await store.create_note(
                CreateNote(title=f"Gone {i}", frontmatter={"type": "reference"})
            )
        ).id
        for i in range(5)
    ]
    for note_id in ids:
        (store.notes_root / (await store.get_note(note_id)).path).unlink()
    scans = 0
    original = type(store)._scan_identities

    def counted(self):
        nonlocal scans
        scans += 1
        return original(self)

    monkeypatch.setattr(type(store), "_scan_identities", counted)
    assert (await store.list_notes(NoteQuery(state="ok"))).items == []
    assert scans == 1


async def test_listing_waits_on_a_note_lock_without_holding_a_pooled_connection(store):
    held = await store.create_note(CreateNote(title="Held", frontmatter={"type": "reference"}))
    other = await store.create_note(CreateNote(title="Other", frontmatter={"type": "reference"}))
    first = min(held.id, other.id)
    other_id = max(held.id, other.id)
    async with store._lock_for(first):
        lists = [asyncio.create_task(store.list_notes(NoteQuery(state="ok"))) for _ in range(14)]
        await asyncio.sleep(0.5)
        assert (await asyncio.wait_for(store.get_note(other_id), timeout=5)).id == other_id
        assert (await asyncio.wait_for(store.get_status(), timeout=5)).counters.filing_failures == 0
    pages = await asyncio.wait_for(asyncio.gather(*lists), timeout=30)
    assert all({n.id for n in page.items} == {held.id, other.id} for page in pages)


async def test_listing_pages_past_unreadable_rows_without_skipping_notes(store):
    ids = []
    for i in range(6):
        ids.append(
            (
                await store.create_note(
                    CreateNote(title=f"Row {i}", frontmatter={"type": "reference"})
                )
            ).id
        )
    ids.sort()
    for note_id in ids[:2]:
        (store.notes_root / (await store.get_note(note_id)).path).unlink()
    first = await store.list_notes(NoteQuery(state="ok", limit=2))
    assert [n.id for n in first.items] == ids[2:4]
    second = await store.list_notes(NoteQuery(state="ok", limit=2, cursor=first.next_cursor))
    assert [n.id for n in second.items] == ids[4:]
    assert second.next_cursor is None


async def test_failed_validation_then_external_delete_is_not_called_a_filing_vanish(store):
    note = await store.create_note(
        CreateNote(
            title="Undated",
            frontmatter={"date": "2026-09-01", "type": "work", "time": "99:99", "reviewed": True},
        )
    )
    assert (await filing.file_reviewed(store))["filing_failed"] == 1
    (store.notes_root / (await store.get_note(note.id)).path).unlink()
    assert (await reconcile_once(store))["missing"] == 1
    row = (await store.list_notes(NoteQuery(state="missing"))).items[0]
    assert row.id == note.id
    assert row.state_reason
    assert "between filing paths" not in row.state_reason


async def test_finalize_runs_even_when_filing_cannot_proceed(store, monkeypatch):
    from coppermind_store import note_lifecycle

    await store.context.setup_lifecycle({"revision": 0})
    result = await store.ingest(ingest_request())
    note = await store.get_note(result.note.id)
    await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"reviewed": True}), note.content_hash
    )
    for source_id in note.sources:
        (store.sources_root / source_id / "manifest.json").unlink()
    assert (await filing.file_reviewed(store))["filing_failed"] == 1
    later = note_lifecycle.clock.monotonic() + 31
    monkeypatch.setattr(note_lifecycle.clock, "monotonic", lambda: later)
    assert (await filing.file_reviewed(store))["filing_failed"] == 1
    assert note.id in store.context.read("finalized", {})


async def test_filing_link_rewrite_does_not_restart_stability_wait(store, monkeypatch):
    from coppermind_store import note_lifecycle

    await store.context.setup_lifecycle({"revision": 0})
    result = await store.ingest(ingest_request())
    note = await store.get_note(result.note.id)
    await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"reviewed": True}), note.content_hash
    )
    assert (await filing.file_reviewed(store))["filing_failed"] == 1
    later = note_lifecycle.clock.monotonic() + 31
    monkeypatch.setattr(note_lifecycle.clock, "monotonic", lambda: later)
    assert (await filing.file_reviewed(store))["filed"] == 1
    assert await filing.file_reviewed(store) == {"filed": 0, "filing_failed": 0}
    filed = await store.get_note(note.id)
    assert filed.filed
    record = store.context.read("finalized", {})[note.id]
    assert (record["path"], record["note_hash"]) == (filed.path, filed.content_hash)
    plan = json.loads((store.control.store.state_dir / "filing" / f"{note.id}.json").read_bytes())
    assert "copies" not in plan
    journal = store.control.store.state_dir / "filing" / f"{note.id}.json"
    assert journal.stat().st_mode & 0o777 == 0o600


async def test_single_note_read_during_move_commit_waits_for_identity(store, monkeypatch):
    from contextlib import asynccontextmanager

    from coppermind.store_protocol import MoveNote

    note = await store.create_note(CreateNote(title="Commit seam"))
    before_commit = asyncio.Event()
    release = asyncio.Event()
    transaction = notes.transaction

    @asynccontextmanager
    async def hold_commit(factory):
        async with transaction(factory) as session:
            yield session
            before_commit.set()
            await release.wait()

    monkeypatch.setattr(notes, "transaction", hold_commit)
    move = asyncio.create_task(store.move_note(note.id, MoveNote(target_folder="filed"), None))
    await asyncio.wait_for(before_commit.wait(), timeout=10)
    read = asyncio.create_task(store.get_note(note.id))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(read), timeout=0.1)
    finally:
        release.set()
    moved, document = await asyncio.gather(move, read)
    assert document.path == moved.path
    assert document.id == note.id


async def test_missing_files_are_not_listed_ok_before_reconciliation(store):
    note = await store.create_note(CreateNote(title="Removed externally"))
    (store.notes_root / note.path).unlink()
    assert not (await store.list_notes(NoteQuery(state="ok"))).items


async def test_existing_old_path_cannot_hide_moved_identity(store):
    note = await store.create_note(CreateNote(title="First note"))
    other = await store.create_note(CreateNote(title="Second note"))
    target = store.notes_root / "moved" / Path(note.path).name
    target.parent.mkdir()
    (store.notes_root / note.path).rename(target)
    (store.notes_root / other.path).rename(store.notes_root / note.path)
    assert (await store.get_note(note.id)).id == note.id
    assert (await store.get_note(note.id)).path == target.relative_to(store.notes_root).as_posix()
    assert (await store.get_note(other.id)).id == other.id


async def test_retry_after_move_publication_failure_counts_the_attempt(store, monkeypatch):
    note = await store.create_note(
        CreateNote(title="Publish retry", frontmatter={"date": "2026-09-01", "reviewed": True})
    )
    publish = filing._publish

    async def fail(*args):
        raise ValueError("publication deferred")

    monkeypatch.setattr(filing, "_publish", fail)
    assert (await filing.file_reviewed(store))["filing_failed"] == 1
    assert (await store.get_note(note.id)).filing_attempts == 1
    monkeypatch.setattr(filing, "_publish", publish)
    assert await filing.file_reviewed(store) == {"filed": 0, "filing_failed": 0}
    recovered = await store.get_note(note.id)
    assert recovered.filed
    assert recovered.filing_attempts == 2
    assert recovered.filing_error is None
    assert (await store.get_status()).counters.filing_failures == 0


async def test_drain_estimate_is_available_before_first_pass_finishes(store, monkeypatch):
    notes = [
        await store.create_note(
            CreateNote(
                title=f"Queue {i}",
                frontmatter={"date": "2026-09-01", "type": "reference", "reviewed": True},
            )
        )
        for i in range(2)
    ]
    entered, release = asyncio.Event(), asyncio.Event()
    file_note = filing.file_note

    async def hold_second(current_store, note_id):
        if note_id == notes[1].id:
            entered.set()
            await release.wait()
        return await file_note(current_store, note_id)

    monkeypatch.setattr(filing, "file_note", hold_second)
    task = asyncio.create_task(filing.file_reviewed(store))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        status = await store.get_status()
        assert status.counters.filing_queue_depth == 1
        assert status.counters.filing_estimated_drain_s is not None
        assert status.counters.filing_estimated_drain_s >= 0
    finally:
        release.set()
        await task
