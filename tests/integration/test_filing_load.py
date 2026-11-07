"""Filing under sustained reads and removal at the filesystem/mirror seam."""

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from coppermind_store import filing, notes
from coppermind_store.reconciler import reconcile_once

from coppermind.store_protocol import (
    CreateNote,
    MetadataUnavailable,
    MoveNote,
    NoteQuery,
    NotesFilesystemUnavailable,
    NoteState,
    NotFound,
    PatchFrontmatter,
)
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


async def _noted_disagreement(store):
    kept = await store.create_note(CreateNote(title="Kept", frontmatter={"type": "reference"}))
    moved = await store.create_note(CreateNote(title="Moved", frontmatter={"type": "reference"}))
    away = store.notes_root / "elsewhere" / Path(kept.path).name
    away.parent.mkdir()
    (store.notes_root / kept.path).rename(away)
    (store.notes_root / moved.path).rename(store.notes_root / kept.path)
    await store.get_note(moved.id)
    notice = store.control.store.state_dir / "path-notices" / f"{moved.id}.json"
    assert notice.exists()
    return kept, moved, notice


async def test_path_notice_clears_when_the_file_returns_to_the_recorded_path(store):
    kept, moved, notice = await _noted_disagreement(store)
    (store.notes_root / kept.path).rename(store.notes_root / moved.path)
    assert not [p for p in await store.get_problems() if p.kind == "path_notice"]
    assert not notice.exists()
    assert (await store.get_status()).counters.active_problems == 0


async def test_path_notice_clears_when_the_note_is_moved_through_the_api(store):
    kept, moved, notice = await _noted_disagreement(store)
    await store.move_note(moved.id, MoveNote(target_folder="archive"), None)
    assert not [p for p in await store.get_problems() if p.kind == "path_notice"]
    assert not notice.exists()
    assert (await store.get_status()).counters.active_problems == 0


async def test_losing_a_repair_race_records_no_notice(store):
    note = await store.create_note(CreateNote(title="Raced", frontmatter={"type": "reference"}))
    away = store.notes_root / "elsewhere" / Path(note.path).name
    away.parent.mkdir()
    (store.notes_root / note.path).rename(away)
    observed = away.relative_to(store.notes_root).as_posix()
    await store.get_note(note.id)
    await store._repair_location(note.id, note.path, observed)
    notice = store.control.store.state_dir / "path-notices" / f"{note.id}.json"
    assert not notice.exists()
    assert not [p for p in await store.get_problems() if p.kind == "path_notice"]


async def test_unreadable_file_is_skipped_by_listing_and_named_as_a_problem(store):
    notes = [
        await store.create_note(CreateNote(title=f"Note {i}", frontmatter={"type": "reference"}))
        for i in range(3)
    ]
    blocked = min(notes, key=lambda n: n.id)
    target = store.notes_root / blocked.path
    target.chmod(0)
    try:
        listed = []
        cursor = None
        while True:
            page = await store.list_notes(NoteQuery(state="ok", limit=1, cursor=cursor))
            listed += [item.id for item in page.items]
            cursor = page.next_cursor
            if cursor is None:
                break
        assert sorted(listed) == sorted(n.id for n in notes if n.id != blocked.id)
        problems = [p for p in await store.get_problems() if p.kind == "unreadable_note"]
        assert [(p.note_id, p.reference) for p in problems] == [(blocked.id, blocked.path)]
        assert "could not be read" in problems[0].reason
        assert (await store.get_status()).counters.active_problems == 1
    finally:
        target.chmod(0o644)
    assert not [p for p in await store.get_problems() if p.kind == "unreadable_note"]
    assert blocked.id in [i.id for i in (await store.list_notes(NoteQuery(state="ok"))).items]
    assert (await store.get_status()).counters.active_problems == 0


async def test_listing_fails_clearly_when_the_whole_notes_filesystem_is_unreadable(store):
    await store.create_note(CreateNote(title="Only", frontmatter={"type": "reference"}))
    store.notes_root.chmod(0)
    try:
        with pytest.raises(NotesFilesystemUnavailable, match="retry shortly"):
            await store.list_notes(NoteQuery(state="ok"))
    finally:
        store.notes_root.chmod(0o755)
    assert len((await store.list_notes(NoteQuery(state="ok"))).items) == 1
    assert not [p for p in await store.get_problems() if p.kind == "unreadable_note"]


async def test_http_listing_pages_past_an_unreadable_note_and_reports_a_storage_outage(store):
    notes_made = [
        await store.create_note(CreateNote(title=f"Http {i}", frontmatter={"type": "reference"}))
        for i in range(3)
    ]
    blocked = min(notes_made, key=lambda n: n.id)
    app, headers = authorized_api(store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api", headers=headers
    ) as client:
        target = store.notes_root / blocked.path
        target.chmod(0)
        try:
            listed, cursor = [], None
            while True:
                params = {"state": "ok", "limit": "1"}
                if cursor:
                    params["cursor"] = cursor
                response = await client.get("/v1/notes", params=params)
                assert response.status_code == 200, response.text
                listed += [item["id"] for item in response.json()["items"]]
                cursor = response.json()["next_cursor"]
                if cursor is None:
                    break
            assert sorted(listed) == sorted(n.id for n in notes_made if n.id != blocked.id)
            assert [
                p.note_id for p in await store.get_problems() if p.kind == "unreadable_note"
            ] == [blocked.id]
        finally:
            target.chmod(0o644)
        store.notes_root.chmod(0)
        try:
            response = await client.get("/v1/notes", params={"state": "ok"})
            assert response.status_code == 503
            assert response.json()["error"] == "notes_filesystem_unavailable"
        finally:
            store.notes_root.chmod(0o755)
        assert (await client.get("/v1/notes", params={"state": "ok"})).status_code == 200


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


def _count_identity_scans(store, monkeypatch):
    scans = []
    original = type(store)._scan_identities

    def counted(self):
        scans.append(1)
        return original(self)

    monkeypatch.setattr(type(store), "_scan_identities", counted)
    return scans


async def test_repeated_reads_of_stale_rows_cost_one_identity_scan(store, monkeypatch):
    gone = [
        await store.create_note(CreateNote(title=f"Gone {i}", frontmatter={"type": "reference"}))
        for i in range(6)
    ]
    for note in gone:
        (store.notes_root / note.path).unlink()
    scans = _count_identity_scans(store, monkeypatch)
    for note in gone:
        with pytest.raises(NotFound):
            await store.get_note(note.id)
    assert (await store.list_notes(NoteQuery(state="ok"))).items == []
    assert len(scans) == 1
    for note in gone:
        with pytest.raises(NotFound):
            await store.get_note(note.id)
    await store.get_status()
    await store.get_problems()
    assert len(scans) == 1


async def test_moved_files_are_followed_from_one_shared_scan(store, monkeypatch):
    made = [
        await store.create_note(CreateNote(title=f"Moved {i}", frontmatter={"type": "reference"}))
        for i in range(4)
    ]
    elsewhere = store.notes_root / "elsewhere"
    elsewhere.mkdir()
    for note in made:
        (store.notes_root / note.path).rename(elsewhere / Path(note.path).name)
    scans = _count_identity_scans(store, monkeypatch)
    for note in made:
        assert (await store.get_note(note.id)).path == f"elsewhere/{Path(note.path).name}"
    assert len(scans) == 1


async def test_a_move_during_an_identity_scan_is_still_followed(store, monkeypatch):
    import threading

    gone = await store.create_note(CreateNote(title="Gone", frontmatter={"type": "reference"}))
    moving = await store.create_note(CreateNote(title="Moving", frontmatter={"type": "reference"}))
    (store.notes_root / gone.path).unlink()
    snapshot_taken, release = threading.Event(), threading.Event()
    calls = []
    original = type(store)._scan_identities

    def slow_first_scan(self):
        calls.append(1)
        found = original(self)
        if len(calls) == 1:
            snapshot_taken.set()
            release.wait(10)
        return found

    monkeypatch.setattr(type(store), "_scan_identities", slow_first_scan)
    first = asyncio.create_task(store.get_note(gone.id))
    await asyncio.to_thread(snapshot_taken.wait, 10)
    elsewhere = store.notes_root / "elsewhere"
    elsewhere.mkdir()
    (store.notes_root / moving.path).rename(elsewhere / Path(moving.path).name)
    second = asyncio.create_task(store.get_note(moving.id))
    await asyncio.sleep(0.3)
    release.set()
    with pytest.raises(NotFound):
        await first
    assert (await second).path == f"elsewhere/{Path(moving.path).name}"
    assert len(calls) == 2


async def test_filing_loop_over_stale_rows_scans_once(store, monkeypatch):
    for i in range(5):
        note = await store.create_note(
            CreateNote(
                title=f"Vanished {i}",
                frontmatter={"date": "2005-01-01", "type": "reference", "reviewed": True},
            )
        )
        (store.notes_root / note.path).unlink()
    scans = _count_identity_scans(store, monkeypatch)
    counts = await filing.file_reviewed(store)
    assert counts["filing_failed"] == 5
    assert len(scans) == 1


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


async def test_missing_filing_copy_can_be_restored_without_claiming_git_removal(store, monkeypatch):
    from coppermind.store_protocol import RestoreNote, ValidationFailed

    note = await store.create_note(
        CreateNote(
            title="Uncommitted seam",
            body="Keep these bytes",
            frontmatter={"date": "2003-06-02", "type": "reference", "reviewed": True},
        )
    )
    original = store.move_note

    async def vanish(*args):
        moved = await original(*args)
        (store.notes_root / moved.path).unlink()
        return moved

    with monkeypatch.context() as patch:
        patch.setattr(store, "move_note", vanish)
        await filing.file_reviewed(store)
    await reconcile_once(store)
    assert [n.id for n in (await store.list_notes(NoteQuery(state="missing"))).items] == [note.id]
    recovery = await store.preview_note_restore(note.id)
    assert recovery.evidence_pending and not recovery.removal_commit
    assert "Keep these bytes" in recovery.content
    # Damaged saved bytes must never become a restore option.
    path = store.control.store.state_dir / "filing" / f"{note.id}.json"
    original_plan = path.read_bytes()
    plan = json.loads(original_plan)
    for copy in plan["copies"]:
        copy["content"] = "dGFtcGVyZWQ="
    path.write_text(json.dumps(plan))
    with pytest.raises(ValidationFailed, match="No verified saved filing copy"):
        await store.preview_note_restore(note.id)
    path.write_bytes(original_plan)
    await store.restore_note(
        note.id, RestoreNote(token=recovery.token, confirmation=f"Restore {note.id}")
    )
    assert (await store.get_note(note.id)).content_hash == recovery.content_hash
    assert not (await store.list_notes(NoteQuery(state="deleted"))).items


async def test_status_separates_failed_and_stalled_filing_from_drainable_work(store):
    from datetime import UTC, datetime, timedelta

    failed = await store.create_note(
        CreateNote(
            title="Failing",
            frontmatter={"date": "2004-01-01", "type": "reference", "reviewed": True},
        )
    )
    await store.create_note(
        CreateNote(
            title="Ready", frontmatter={"date": "2005-01-01", "type": "reference", "reviewed": True}
        )
    )
    directory = store.control.store.state_dir
    (directory / "filing").mkdir(exist_ok=True)
    (directory / "filing" / f"{failed.id}.json").write_text(
        json.dumps({"error": "NotesFilesystemUnavailable", "attempts": 2})
    )
    now = datetime.now(UTC)
    (directory / "filing-rate.json").write_text(
        json.dumps({"seconds_per_note": 60, "last_progress_at": now.isoformat()})
    )
    counters = (await store.get_status()).counters
    assert (counters.filing_queue_depth, counters.filing_ready_depth, counters.filing_failures) == (
        2,
        1,
        1,
    )
    assert counters.filing_estimated_drain_s == 60
    problem = next(p for p in await store.get_problems() if p.kind == "filing")
    assert problem.filing_attempts == 2 and problem.reference == failed.path
    assert "NotesFilesystemUnavailable" not in problem.reason
    assert "destination folder" in problem.reason
    long_ago = (now - timedelta(hours=1)).isoformat()
    (directory / "filing-rate.json").write_text(
        json.dumps({"seconds_per_note": 60, "last_progress_at": long_ago})
    )
    counters = (await store.get_status()).counters
    assert counters.filing_stalled and counters.filing_estimated_drain_s is None
    (directory / "filing-rate.json").write_text(
        json.dumps(
            {
                "seconds_per_note": 60,
                "last_progress_at": long_ago,
                "last_pass_at": now.isoformat(),
            }
        )
    )
    counters = (await store.get_status()).counters
    assert not counters.filing_stalled and counters.filing_estimated_drain_s == 60
    (directory / "filing-rate.json").write_text(
        json.dumps(
            {
                "seconds_per_note": 60,
                "last_progress_at": (now - timedelta(hours=1)).isoformat(),
                "stuck": True,
            }
        )
    )
    counters = (await store.get_status()).counters
    assert counters.filing_stalled and counters.filing_estimated_drain_s is None
    (directory / "filing-rate.json").write_text(json.dumps({"stuck": True}))
    counters = (await store.get_status()).counters
    assert counters.filing_stalled and counters.filing_last_progress_at is None


async def test_filing_problem_names_what_is_wrong_with_the_note(store):
    note = await store.create_note(
        CreateNote(
            title="Meeting",
            frontmatter={"type": "reference", "reviewed": True, "date": "2005-01-01"},
        )
    )
    directory = store.control.store.state_dir / "filing"
    directory.mkdir(exist_ok=True)
    (directory / f"{note.id}.json").write_text(
        json.dumps({"error": "ValueError: enter a valid local meeting time", "attempts": 3})
    )
    problem = next(p for p in await store.get_problems() if p.kind == "filing")
    assert problem.note_id == note.id and problem.reference == note.path
    assert "enter a valid local meeting time" in problem.reason
    (directory / f"{note.id}.json").write_text(
        json.dumps({"error": "FileNotFoundError", "attempts": 1})
    )
    problem = next(p for p in await store.get_problems() if p.kind == "filing")
    assert "date" not in problem.reason and "FileNotFoundError" not in problem.reason


async def test_a_pass_that_cannot_file_ready_work_marks_it_stalled_until_progress(
    store, monkeypatch
):
    from coppermind_store.filing import file_reviewed

    await store.create_note(
        CreateNote(
            title="Ready", frontmatter={"date": "2005-01-01", "type": "reference", "reviewed": True}
        )
    )

    async def decline(*args):
        return False

    real = filing.file_note
    monkeypatch.setattr(filing, "file_note", decline)
    await file_reviewed(store)
    counters = (await store.get_status()).counters
    assert counters.filing_stalled and counters.filing_estimated_drain_s is None
    monkeypatch.setattr(filing, "file_note", real)
    await file_reviewed(store)
    counters = (await store.get_status()).counters
    assert not counters.filing_stalled and counters.filing_queue_depth == 0


async def test_a_pass_that_files_the_ready_work_leaves_nothing_stalled(store):
    from coppermind_store.filing import file_reviewed

    await store.create_note(
        CreateNote(
            title="Ready", frontmatter={"date": "2005-01-01", "type": "reference", "reviewed": True}
        )
    )
    await file_reviewed(store)
    rate = json.loads((store.control.store.state_dir / "filing-rate.json").read_bytes())
    assert rate["stuck"] is False and "last_progress_at" in rate
    counters = (await store.get_status()).counters
    assert not counters.filing_stalled and counters.filing_queue_depth == 0


async def test_an_unreadable_subfolder_types_the_identity_walk_as_the_notes_outage(store):
    """The identity walk's ``OSError`` arrives typed rather than as a bare 500.

    A note whose file sits at a path the mirror does not record makes a read
    follow its identity, and that is the one step which walks the tree. With the
    folder made unreadable the walk raises an ``OSError`` from inside
    ``asyncio.to_thread``, so the index boundary has to convert it: the read then
    answers the documented 503 code instead of a 500.
    """
    note = await store.create_note(CreateNote(title="Folded", frontmatter={"type": "reference"}))
    elsewhere = store.notes_root / "elsewhere"
    elsewhere.mkdir()
    (store.notes_root / note.path).rename(elsewhere / Path(note.path).name)
    elsewhere.chmod(0)
    try:
        store._identity_index._found = None
        store._identity_index._built = 0.0
        with pytest.raises(NotesFilesystemUnavailable) as raised:
            await store._identity_index.refresh()
        assert "Permission denied" in str(raised.value)
        with pytest.raises(NotesFilesystemUnavailable):
            await store.get_note(note.id)
    finally:
        elsewhere.chmod(0o755)
    assert (await store.get_note(note.id)).path == f"elsewhere/{Path(note.path).name}"


async def test_a_missing_notes_root_types_the_identity_walk_on_every_path(store):
    """No directory to walk is the same outage for the single read and the listing.

    The mirror's rows survive an unreadable root, so both paths reach the
    identity index with nothing to scan and must give one and the same code.
    """
    note = await store.create_note(CreateNote(title="Rooted", frontmatter={"type": "reference"}))
    elsewhere = store.notes_root / "elsewhere"
    elsewhere.mkdir()
    (store.notes_root / note.path).rename(elsewhere / Path(note.path).name)
    await store.get_note(note.id)

    root = store.notes_root
    root.chmod(0)
    try:
        store._identity_index._found = None
        store._identity_index._built = 0.0
        with pytest.raises(NotesFilesystemUnavailable):
            await store.list_notes(NoteQuery(state="ok"))
        with pytest.raises(NotesFilesystemUnavailable):
            await store.get_note(note.id)
    finally:
        root.chmod(0o755)
    assert (await store.list_notes(NoteQuery(state="ok"))).items[0].id == note.id


async def test_a_listing_walks_the_tree_once_however_many_notes_it_reads(store):
    """One walk answers the whole batch rather than one per note read."""
    for index in range(3):
        await store.create_note(
            CreateNote(title=f"Walk {index}", frontmatter={"type": "reference"})
        )

    with patch.object(store, "_scan_identities", wraps=store._scan_identities) as walk:
        listed = await store.list_notes(NoteQuery(state="ok"))

    assert len(listed.items) == 3
    assert walk.call_count == 1


async def test_a_vault_whose_only_note_is_unreadable_still_lists_and_names_the_problem(store):
    """One unreadable file is that note's problem, not an outage of the whole volume."""
    note = await store.create_note(CreateNote(title="Alone", frontmatter={"type": "reference"}))
    target = store.notes_root / note.path
    target.chmod(0)
    try:
        listed = await store.list_notes(NoteQuery(state="ok"))
        assert listed.items == []
        problems = [p for p in await store.get_problems() if p.kind == "unreadable_note"]
        assert [(p.note_id, p.reference) for p in problems] == [(note.id, note.path)]
    finally:
        target.chmod(0o644)
    assert [item.id for item in (await store.list_notes(NoteQuery(state="ok"))).items] == [note.id]


async def test_an_unreadable_subfolder_stops_the_listing_on_the_notes_outage(store):
    """A listing that has to walk answers the outage rather than a short page.

    With the mirror still naming the pre-move path and the only walk made
    unreadable, the listing cannot say what it found, so it keeps the 503 code
    the scan reported instead of returning the one row that did resolve.
    """
    note = await store.create_note(CreateNote(title="Moving", frontmatter={"type": "reference"}))
    elsewhere = store.notes_root / "elsewhere"
    elsewhere.mkdir()
    (store.notes_root / note.path).rename(elsewhere / Path(note.path).name)
    await store.get_note(note.id)

    elsewhere.chmod(0)
    try:
        store._identity_index._found = None
        store._identity_index._built = 0.0
        with pytest.raises(NotesFilesystemUnavailable):
            await store.list_notes(NoteQuery(state="ok"))
    finally:
        elsewhere.chmod(0o755)

    listed = await store.list_notes(NoteQuery(state="ok"))
    assert [item.id for item in listed.items] == [note.id]


async def test_the_notes_outage_keeps_its_code_beside_the_metadata_one(store, unreachable_store):
    """The two outages stay distinct: one unreadable folder, then a database away.

    The identity walk shares the ``OSError`` shape with the mirror's own failures,
    so this pins that the walk keeps `notes_filesystem_unavailable` where a
    database that is not there still answers `metadata_unavailable`.
    """
    note = await store.create_note(CreateNote(title="Typed", frontmatter={"type": "reference"}))
    elsewhere = store.notes_root / "elsewhere"
    elsewhere.mkdir()
    (store.notes_root / note.path).rename(elsewhere / Path(note.path).name)
    await store.get_note(note.id)
    elsewhere.chmod(0)
    try:
        store._identity_index._found = None
        store._identity_index._built = 0.0
        with pytest.raises(NotesFilesystemUnavailable):
            await store.get_note(note.id)
    finally:
        elsewhere.chmod(0o755)

    with pytest.raises(MetadataUnavailable):
        await unreachable_store.list_notes(NoteQuery())
