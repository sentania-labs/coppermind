"""Reconciliation of known and device-created notes from the notes filesystem."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from coppermind_store import reconciler
from coppermind_store.fs import content_hash
from coppermind_store.notes import LocalStore
from coppermind_store.reconciler import UNPARSED_REASON, UNREADABLE_REASON, reconcile_once

from coppermind import frontmatter as fm
from coppermind.db.models import Note
from coppermind.ids import is_valid_id
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

    counts = await reconcile_once(store)
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

    counts = await reconcile_once(store)
    fetched = await store.get_note(note.id)

    assert counts["moved"] == 1
    assert fetched.id == note.id
    assert fetched.path == "Work/Operations Runbook.md"
    assert (await _row(store, note.id)).path == fetched.path


async def test_device_delete_is_missing_instead_of_present(store: LocalStore):
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    (store.notes_root / note.path).unlink()

    counts = await reconcile_once(store)
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

    await reconcile_once(store)

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


async def test_a_settled_device_created_file_is_adopted_and_retrievable(store: LocalStore):
    unknown = store.notes_root / "Review" / "Made on phone.md"
    unknown.parent.mkdir(parents=True, exist_ok=True)
    body = "# Made on phone\r\n\r\nNo identity yet.\r\n"
    original = (
        "---\r\n"
        "tags: [phone] # keep this style\r\n"
        "my_key: hand-written # keep this comment\r\n"
        "---\r\n"
        f"{body}"
    ).encode()
    unknown.write_bytes(original)

    counts = await reconcile_once(store)
    adopted_text = unknown.read_bytes().decode("utf-8")
    frontmatter, adopted_body = fm.parse(adopted_text)
    note_id = frontmatter[store.control.schema().role("id_key")]
    fetched = await store.get_note(note_id)

    assert is_valid_id(note_id)
    assert adopted_body == body
    assert (
        adopted_text.index("tags: [phone] # keep this style")
        < adopted_text.index("my_key: hand-written # keep this comment")
        < adopted_text.index("schema_version: 1")
    )
    assert "\n" not in adopted_text.replace("\r\n", "")
    assert fetched.id == note_id
    assert fetched.path == "Review/Made on phone.md"
    assert fetched.body == body
    assert counts["adopted"] == 1


async def test_a_device_created_file_waits_for_quiet_before_adoption(store: LocalStore):
    unknown = store.notes_root / "Review" / "Still syncing.md"
    unknown.parent.mkdir(parents=True, exist_ok=True)
    original = b"# Still syncing\n\nFirst piece.\n"
    unknown.write_bytes(original)
    remembered: reconciler.UnidentifiedStats = {}

    waiting = await reconcile_once(store, quiet_period_s=3600, unidentified=remembered)

    assert waiting["adopted"] == 0
    assert waiting["deferred"] == 1
    assert unknown.read_bytes() == original
    async with store.session_factory() as session:
        assert (await session.scalar(sa.select(sa.func.count()).select_from(Note))) == 0

    settled = await reconcile_once(store, unidentified=remembered)
    frontmatter, body = fm.parse(unknown.read_text(encoding="utf-8"))

    assert settled["adopted"] == 1
    assert body == original.decode()


async def test_adoption_never_overwrites_a_device_write(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
):
    unknown = store.notes_root / "Review" / "Racing sync.md"
    unknown.parent.mkdir(parents=True, exist_ok=True)
    unknown.write_text("# Racing sync\n\nFirst piece.\n", encoding="utf-8")
    real_adopt = store.adopt_note

    async def device_writes_first(relative, expected_hash, *, replace_id=None):
        unknown.write_text("# Racing sync\n\nFirst piece.\nSecond piece.\n", encoding="utf-8")
        return await real_adopt(relative, expected_hash, replace_id=replace_id)

    monkeypatch.setattr(store, "adopt_note", device_writes_first)
    counts = await reconcile_once(store)

    assert counts["adopted"] == 0
    assert counts["deferred"] == 1
    assert unknown.read_text(encoding="utf-8") == "# Racing sync\n\nFirst piece.\nSecond piece.\n"


async def test_an_invalid_device_created_file_is_reported_and_left_byte_exact(
    store: LocalStore,
):
    unknown = store.notes_root / "Review" / "Needs repair.md"
    unknown.parent.mkdir(parents=True, exist_ok=True)
    original = b"---\ntype: not-a-real-type # keep this\n---\n# Needs repair\n"
    unknown.write_bytes(original)

    counts = await reconcile_once(store)

    assert counts["adopted"] == 0
    assert counts["unparsed"] == 1
    assert unknown.read_bytes() == original
    async with store.session_factory() as session:
        assert (await session.scalar(sa.select(sa.func.count()).select_from(Note))) == 0


async def test_a_copied_identity_gets_a_fresh_id_without_changing_the_owner(
    store: LocalStore,
):
    original = await store.create_note(
        CreateNote(title="Runbook", frontmatter={"type": "reference"})
    )
    original_path = store.notes_root / original.path
    copied_path = original_path.with_name("Runbook copy.md")
    copied_path.write_bytes(original_path.read_bytes())

    counts = await reconcile_once(store)
    copied_frontmatter, _ = fm.parse(copied_path.read_text(encoding="utf-8"))
    copied_id = copied_frontmatter[store.control.schema().role("id_key")]

    assert counts["adopted"] == 1
    assert copied_id != original.id
    assert (await store.get_note(original.id)).path == original.path
    assert (await store.get_note(copied_id)).path == "Review/Runbook copy.md"


async def test_a_file_no_longer_utf8_is_unparsed_at_its_path_not_missing(store: LocalStore):
    """A device that broke the bytes has not deleted the note."""
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    path = store.notes_root / note.path
    broken = path.read_bytes().replace(b"# Runbook", b"# Runb\xffok")
    path.write_bytes(broken)

    counts = await reconcile_once(store)
    row = await _row(store, note.id)

    assert counts == {
        "adopted": 0,
        "changed": 0,
        "moved": 0,
        "missing": 0,
        "unparsed": 1,
        "deferred": 0,
    }
    assert row.state == "unparsed"
    assert row.path == note.path
    assert row.content_hash == content_hash(broken)
    assert row.size_bytes == len(broken)
    assert [item.id for item in (await store.list_notes(NoteQuery(state="unparsed"))).items] == [
        note.id
    ]
    with pytest.raises(NoteUnparseable):
        await store.get_note(note.id)


async def test_a_break_that_takes_the_id_line_still_holds_the_known_path(store: LocalStore):
    """Identity is unrecoverable, so the row that names the path claims the file."""
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    path = store.notes_root / note.path
    text = path.read_text(encoding="utf-8").replace(
        f"id: {note.id}", f"id: {note.id} # mine\nid: ["
    )
    path.write_text(text, encoding="utf-8")

    await reconcile_once(store)
    row = await _row(store, note.id)

    assert row.state == "unparsed"
    assert row.path == note.path
    assert row.content_hash == content_hash(path.read_bytes())


@pytest.mark.parametrize("case", ["unidentifiable", "quiet-window-move", "stripped-identity"])
async def test_a_file_present_at_a_known_path_is_never_reported_missing(
    store: LocalStore, case: str
):
    """Missing means the scan observed absence, not uncertainty about a present file."""
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    path = store.notes_root / note.path
    quiet_period_s = 0

    if case == "unidentifiable":
        path.write_text("---\nreviewed: [\n---\n\n# Runbook\n", encoding="utf-8")
    elif case == "quiet-window-move":
        moved = await store.create_note(
            CreateNote(title="Meeting", frontmatter={"type": "reference"})
        )
        path.unlink()
        (store.notes_root / moved.path).rename(path)
        quiet_period_s = 3600
    else:
        text = path.read_text(encoding="utf-8").replace(f"id: {note.id}\n", "")
        path.write_text(text, encoding="utf-8")

    counts = await reconcile_once(store, quiet_period_s=quiet_period_s)
    row = await _row(store, note.id)

    assert path.exists()
    assert counts["missing"] == 0
    assert row.state != "missing"
    assert row.path == note.path
    if case != "quiet-window-move":
        assert row.state == "unparsed"


async def test_a_file_replaced_between_stat_and_read_defers_missing(store: LocalStore, monkeypatch):
    """A vanishing directory entry during a read is uncertainty, not absence."""
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    path = store.notes_root / note.path
    path.write_bytes(path.read_bytes().replace(b"# Runbook", b"# Current Runbook"))
    real_read = Path.read_bytes
    raced = False

    def replace_while_reading(target: Path) -> bytes:
        nonlocal raced
        if target == path and not raced:
            raced = True
            replacement = real_read(target)
            target.unlink()
            target.write_bytes(replacement)
            raise FileNotFoundError(target)
        return real_read(target)

    monkeypatch.setattr(Path, "read_bytes", replace_while_reading)

    deferred = await reconcile_once(store)
    row = await _row(store, note.id)

    assert deferred["deferred"] == 1
    assert deferred["missing"] == 0
    assert path.exists()
    assert row.state != "missing"

    settled = await reconcile_once(store)
    assert settled["changed"] == 1
    assert (await _row(store, note.id)).title == "Current Runbook"


async def test_two_live_copies_leave_the_row_alone_instead_of_reporting_it_gone(
    store: LocalStore,
):
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    original = store.notes_root / note.path
    copy = store.notes_root / "Copy.md"
    moved = store.notes_root / "Moved.md"
    copy.write_bytes(original.read_bytes())
    original.rename(moved)

    counts = await reconcile_once(store)
    row = await _row(store, note.id)

    assert counts == {
        "adopted": 0,
        "changed": 0,
        "moved": 0,
        "missing": 0,
        "unparsed": 0,
        "deferred": 0,
    }
    assert row.state == "ok"
    assert row.path == note.path


async def test_one_unreadable_file_does_not_stop_the_others_converging(store: LocalStore):
    """A durable per-file fault is its own state, not a missing note or a dead scan."""
    unreadable = await store.create_note(
        CreateNote(title="Runbook", frontmatter={"type": "reference"})
    )
    healthy = await store.create_note(
        CreateNote(title="Meeting", frontmatter={"type": "reference"})
    )
    broken_path = store.notes_root / unreadable.path
    broken_path.unlink()
    broken_path.symlink_to(broken_path.name)
    healthy_path = store.notes_root / healthy.path
    healthy_path.write_bytes(healthy_path.read_bytes().replace(b"# Meeting", b"# Standup"))

    await reconcile_once(store)

    unreadable_row = await _row(store, unreadable.id)
    assert unreadable_row.state == "unparsed"
    assert unreadable_row.state_reason == UNREADABLE_REASON
    assert unreadable_row.path == unreadable.path
    assert (await _row(store, healthy.id)).title == "Standup"


async def test_an_interval_scan_trusts_a_stat_and_the_daily_rehash_does_not(store: LocalStore):
    """The steady-state pass costs a stat per file; the full pass rereads them."""
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    await reconcile_once(store)
    path = store.notes_root / note.path
    before = path.stat()
    path.write_bytes(path.read_bytes().replace(b"# Runbook", b"# Runbouk"))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size

    assert await reconcile_once(store) == {
        "adopted": 0,
        "changed": 0,
        "moved": 0,
        "missing": 0,
        "unparsed": 0,
        "deferred": 0,
    }
    assert (await _row(store, note.id)).title == "Runbook"

    assert await reconcile_once(store, full=True) == {
        "adopted": 0,
        "changed": 1,
        "moved": 0,
        "missing": 0,
        "unparsed": 0,
        "deferred": 0,
    }
    assert (await _row(store, note.id)).title == "Runbouk"


async def test_a_file_still_inside_the_quiet_period_waits_rather_than_going_missing(
    store: LocalStore,
):
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    path = store.notes_root / note.path
    path.write_bytes(path.read_bytes().replace(b"# Runbook", b"# Current Runbook"))

    deferred = await reconcile_once(store, quiet_period_s=3600)
    settling = await _row(store, note.id)

    assert deferred == {
        "adopted": 0,
        "changed": 0,
        "moved": 0,
        "missing": 0,
        "unparsed": 0,
        "deferred": 1,
    }
    assert settling.state == "ok"
    assert settling.title == "Runbook"

    assert await reconcile_once(store) == {
        "adopted": 0,
        "changed": 1,
        "moved": 0,
        "missing": 0,
        "unparsed": 0,
        "deferred": 0,
    }
    assert (await _row(store, note.id)).title == "Current Runbook"


async def test_a_move_still_being_typed_on_is_followed_not_reported_deleted(store: LocalStore):
    """A file at a path no row claims is read, quiet period or not."""
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    moved = store.notes_root / "Work" / "Runbook.md"
    moved.parent.mkdir()
    (store.notes_root / note.path).rename(moved)
    moved.write_bytes(moved.read_bytes().replace(b"# Runbook", b"# Runbook in progress"))

    counts = await reconcile_once(store, quiet_period_s=3600)
    row = await _row(store, note.id)

    assert counts["missing"] == 0
    assert row.state == "ok"
    assert row.path == "Work/Runbook.md"
    assert (await store.get_note(note.id)).path == "Work/Runbook.md"


async def test_a_stranger_at_the_old_path_does_not_capture_the_note_that_moved(
    store: LocalStore,
):
    """An identity the file named itself outranks one inherited from a path."""
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    old_path = store.notes_root / note.path
    moved = store.notes_root / "Work" / "Operations Runbook.md"
    moved.parent.mkdir()
    old_path.rename(moved)
    old_path.write_text("---\nreviewed: [\n---\n\n# Typed on a phone\n", encoding="utf-8")

    await reconcile_once(store)
    row = await _row(store, note.id)

    assert row.state == "ok"
    assert row.path == "Work/Operations Runbook.md"
    assert (await store.get_note(note.id)).title == "Runbook"


async def test_a_directory_where_the_note_was_is_unparsed_not_missing(store: LocalStore):
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    path = store.notes_root / note.path
    path.unlink()
    path.mkdir()

    await reconcile_once(store)
    row = await _row(store, note.id)

    assert row.state == "unparsed"
    assert row.state_reason == UNREADABLE_REASON
    assert row.path == note.path


async def test_a_note_symlinked_out_of_the_notes_filesystem_is_unparsed_not_missing(
    store: LocalStore,
):
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    outside = store.notes_root.parent / "elsewhere.md"
    path = store.notes_root / note.path
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)

    await reconcile_once(store)
    row = await _row(store, note.id)

    assert row.state == "unparsed"
    assert row.state_reason == UNREADABLE_REASON
    assert row.path == note.path


async def test_a_swap_inside_the_quiet_period_reports_neither_note_deleted(store: LocalStore):
    """A pass that left a file unread cannot tell whose bytes are at its path."""
    deleted = await store.create_note(
        CreateNote(title="Runbook", frontmatter={"type": "reference"})
    )
    moved = await store.create_note(CreateNote(title="Meeting", frontmatter={"type": "reference"}))
    occupied = store.notes_root / deleted.path
    occupied.unlink()
    (store.notes_root / moved.path).rename(occupied)

    for _ in range(3):
        os.utime(occupied, None)
        counts = await reconcile_once(store, quiet_period_s=3600)
        assert counts["missing"] == 0
        assert (await _row(store, deleted.id)).state != "missing"
        assert (await _row(store, moved.id)).state != "missing"
        assert {item.state for item in (await store.list_notes(NoteQuery())).items} == {"ok"}

    settled = await reconcile_once(store)

    assert settled["missing"] == 1
    assert (await _row(store, deleted.id)).state == "missing"
    moved_row = await _row(store, moved.id)
    assert moved_row.state == "ok"
    assert moved_row.path == deleted.path
    assert (await store.get_note(moved.id)).id == moved.id


async def test_an_unreadable_note_says_why_its_hash_is_not_current(store: LocalStore):
    """A summary carries the reason, so a stale ETag is distinguishable."""
    unreadable = await store.create_note(
        CreateNote(title="Runbook", frontmatter={"type": "reference"})
    )
    broken = await store.create_note(CreateNote(title="Meeting", frontmatter={"type": "reference"}))
    unreadable_path = store.notes_root / unreadable.path
    last_read_hash = content_hash(unreadable_path.read_bytes())
    unreadable_path.unlink()
    unreadable_path.symlink_to(unreadable_path.name)
    broken_path = store.notes_root / broken.path
    broken_path.write_bytes(broken_path.read_bytes().replace(b"tags: []", b"tags: ["))

    await reconcile_once(store)
    summaries = {
        item.id: item for item in (await store.list_notes(NoteQuery(state="unparsed"))).items
    }

    assert summaries[unreadable.id].state_reason == UNREADABLE_REASON
    assert summaries[unreadable.id].content_hash == last_read_hash
    assert summaries[broken.id].state_reason == UNPARSED_REASON
    assert summaries[broken.id].content_hash == content_hash(broken_path.read_bytes())


async def test_a_copy_made_after_a_scan_does_not_steal_the_row(store: LocalStore):
    """A stat-credited note is still present, so a copy of it is a second copy."""
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    await reconcile_once(store)
    original = store.notes_root / note.path
    copy = original.with_name("Runbook 1.md")
    copy.write_bytes(original.read_bytes())

    counts = await reconcile_once(store)
    row = await _row(store, note.id)

    assert counts["moved"] == 0
    assert row.path == note.path
    assert row.state == "ok"
    assert (await store.get_note(note.id)).path == note.path
    assert original.exists()

    await reconcile_once(store, full=True)

    assert (await _row(store, note.id)).path == note.path


async def test_a_file_stamped_in_the_future_does_not_stop_deletions_being_reported(
    store: LocalStore,
):
    """A wrong clock is not an in-flight write, so it defers nothing."""
    stamped = await store.create_note(
        CreateNote(title="Runbook", frontmatter={"type": "reference"})
    )
    deleted = await store.create_note(
        CreateNote(title="Meeting", frontmatter={"type": "reference"})
    )
    ahead = (datetime.now(tz=UTC) + timedelta(days=365)).timestamp()
    os.utime(store.notes_root / stamped.path, (ahead, ahead))
    (store.notes_root / deleted.path).unlink()

    counts = await reconcile_once(store, quiet_period_s=3600)

    assert counts["deferred"] == 0
    assert counts["missing"] == 1
    assert (await _row(store, deleted.id)).state == "missing"
    assert (await _row(store, stamped.id)).state == "ok"


async def test_a_conflict_copy_cannot_capture_a_row_whose_file_is_settling(store: LocalStore):
    """A deferred file is still the note's own file, so a copy is a second copy."""
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    await reconcile_once(store)
    original = store.notes_root / note.path
    original.write_bytes(original.read_bytes().replace(b"# Runbook", b"# Runbook being typed"))
    conflict = original.with_name("Runbook (conflict 2026-09-15).md")
    conflict.write_bytes(original.read_bytes())

    for _ in range(2):
        counts = await reconcile_once(store, quiet_period_s=3600)
        row = await _row(store, note.id)
        assert counts["moved"] == 0
        assert counts["deferred"] == 2
        assert row.path == note.path
        assert (await store.get_note(note.id)).path == note.path

    assert original.exists()


async def test_a_file_written_during_a_slow_pass_is_still_deferred(store: LocalStore, monkeypatch):
    """The quiet window closes at the clock when a file is stat'd, not at the start."""
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    await reconcile_once(store)
    path = store.notes_root / note.path
    real_index = reconciler._mirror_index

    async def index_then_a_device_writes(target):
        index = await real_index(target)
        await asyncio.sleep(1.5)
        path.write_bytes(path.read_bytes().replace(b"# Runbook", b"# Runbook in progress"))
        return index

    monkeypatch.setattr(reconciler, "_mirror_index", index_then_a_device_writes)
    counts = await reconcile_once(store, quiet_period_s=3600)

    assert counts["deferred"] == 1
    assert counts["changed"] == 0
    assert (await _row(store, note.id)).title == "Runbook"


async def test_a_rejected_identity_is_read_once_then_stat_trusted(store: LocalStore, monkeypatch):
    """An unchanged rejected file must not cost a parse per pass."""
    unknown = store.notes_root / "Review" / "Made on phone.md"
    unknown.parent.mkdir(parents=True, exist_ok=True)
    unknown.write_bytes(b"---\nid: invalid\n---\n# Made on phone\n")
    remembered: reconciler.UnidentifiedStats = {}
    read: list[str] = []
    real_observe = reconciler._observe

    def counted(safe_path, relative, data, mtime, schema, by_id, entry, *, settled):
        read.append(relative)
        return real_observe(safe_path, relative, data, mtime, schema, by_id, entry, settled=settled)

    monkeypatch.setattr(reconciler, "_observe", counted)

    await reconcile_once(store, unidentified=remembered)
    assert read == ["Review/Made on phone.md"]

    await reconcile_once(store, unidentified=remembered)
    assert read == ["Review/Made on phone.md"]

    await reconcile_once(store, unidentified=remembered, full=True)
    assert len(read) == 2

    unknown.write_bytes(b"---\nid: invalid\n---\n# Made on phone\n\nEdited on the device.\n")
    await reconcile_once(store, unidentified=remembered)
    assert len(read) == 3
    assert unknown.read_bytes().endswith(b"Edited on the device.\n")


async def test_a_forgotten_path_stops_being_remembered_once_its_file_is_gone(
    store: LocalStore,
):
    unknown = store.notes_root / "Review" / "Made on phone.md"
    unknown.parent.mkdir(parents=True, exist_ok=True)
    unknown.write_bytes(b"---\nid: invalid\n---\n# Made on phone\n")
    remembered: reconciler.UnidentifiedStats = {}

    await reconcile_once(store, unidentified=remembered)
    assert "Review/Made on phone.md" in remembered

    unknown.unlink()
    await reconcile_once(store, unidentified=remembered)

    assert remembered == {}


async def test_a_known_note_moved_to_an_unremembered_path_is_still_identified(
    store: LocalStore,
):
    """The stat shortcut must never hide a note that arrived somewhere new."""
    note = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    remembered: reconciler.UnidentifiedStats = {}
    await reconcile_once(store, unidentified=remembered)
    moved = store.notes_root / "Work" / "Operations Runbook.md"
    moved.parent.mkdir()
    (store.notes_root / note.path).rename(moved)

    counts = await reconcile_once(store, unidentified=remembered)

    assert counts["moved"] == 1
    assert (await _row(store, note.id)).path == "Work/Operations Runbook.md"
    assert (await store.get_note(note.id)).path == "Work/Operations Runbook.md"
