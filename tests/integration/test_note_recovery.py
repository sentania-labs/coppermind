"""Actual Git commits, filesystem transitions and PostgreSQL reconstruction."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import sqlalchemy as sa
from coppermind_store.notes import LocalStore
from coppermind_store.reconciler import reconcile_once

from coppermind.db.models import Note
from coppermind.store_protocol import (
    CreateNote,
    NoteQuery,
    PathCollision,
    RestoreNote,
    ValidationFailed,
)


def git(root: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def commit(root: Path) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Snapshot")
    return git(root, "rev-parse", "HEAD")


async def deleted(store: LocalStore):
    note = await store.create_note(CreateNote(title="Recovery", body="Original evidence"))
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    path = store.notes_root / note.path
    data = path.read_bytes()
    path.unlink()
    await reconcile_once(store)
    assert (await store.list_notes(NoteQuery(state="missing"))).items[0].id == note.id
    removal = commit(store.notes_root)
    counts = await reconcile_once(store)
    assert counts["deleted"] == 1
    return note, data, removal


async def test_committed_removal_preview_restore_and_rebuild(store: LocalStore):
    note, data, removal = await deleted(store)
    preview = await store.preview_note_restore(note.id)
    assert preview.content.encode() == data
    assert preview.removal_commit == removal
    assert (await store.get_status()).counters.notes_awaiting_review == 0
    # Reconstruct a lost mirror from receipts before scanning returning files.
    async with store.session_factory() as session:
        await session.execute(sa.delete(Note))
        await session.commit()
    await store.rebuild_metadata()
    assert (await store.list_notes(NoteQuery(state="deleted"))).items[0].id == note.id
    await store.restore_note(
        note.id, RestoreNote(token=preview.token, confirmation=f"Restore {note.id}")
    )
    assert (store.notes_root / note.path).read_bytes() == data
    assert (await store.get_note(note.id)).id == note.id
    assert (await store.list_notes(NoteQuery(state="deleted"))).items == []
    audit = list((store.control.store.state_dir / "note-recovery" / "restores").glob("*.json"))
    assert f"Restore {note.id}" in audit[0].read_text()


async def test_return_at_new_path_recovers_same_id(store: LocalStore):
    note, data, _ = await deleted(store)
    returned = store.notes_root / "Elsewhere.md"
    returned.write_bytes(data)
    await reconcile_once(store)
    assert (await store.get_note(note.id)).path == "Elsewhere.md"
    with pytest.raises(ValidationFailed):
        await store.preview_note_restore(note.id)


async def test_occupied_destination_is_never_overwritten(store: LocalStore):
    note, _, _ = await deleted(store)
    preview = await store.preview_note_restore(note.id)
    stranger = await store.create_note(CreateNote(title="Stranger"))
    path = store.notes_root / note.path
    path.write_bytes((store.notes_root / stranger.path).read_bytes())
    # Let both files settle and remove the original to avoid an intentional duplicate.
    (store.notes_root / stranger.path).unlink()
    os.utime(path, (1, 1))
    original = path.read_bytes()
    with pytest.raises(PathCollision):
        await store.restore_note(
            note.id, RestoreNote(token=preview.token, confirmation=f"Restore {note.id}")
        )
    assert path.read_bytes() == original


async def test_uncommitted_latest_contents_remain_missing(store: LocalStore):
    note = await store.create_note(CreateNote(title="Unsaved snapshot"))
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    path = store.notes_root / note.path
    path.write_bytes(path.read_bytes() + b"Uncaptured edit\n")
    await reconcile_once(store)
    path.unlink()
    commit(store.notes_root)
    await reconcile_once(store)
    assert (await store.list_notes(NoteQuery(state="missing"))).items[0].id == note.id
    assert (await store.list_notes(NoteQuery(state="deleted"))).items == []


async def test_move_committed_as_delete_and_add_is_not_deletion(store: LocalStore):
    note = await store.create_note(CreateNote(title="Moved"))
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    target = store.notes_root / "Moved.md"
    (store.notes_root / note.path).rename(target)
    commit(store.notes_root)
    await reconcile_once(store)
    assert (await store.get_note(note.id)).path == "Moved.md"
    assert not (await store.list_notes(NoteQuery(state="deleted"))).items


async def test_git_failure_does_not_block_observation_or_erase_recovery(store: LocalStore):
    note, _, _ = await deleted(store)
    git_dir = store.notes_root / ".git"
    git_dir.rename(store.notes_root / ".git-offline")
    await reconcile_once(store)
    assert (await store.list_notes(NoteQuery(state="deleted"))).items[0].id == note.id
    assert not (await store.get_note_history_status()).available
    assert (await store.preview_note_restore(note.id)).content


async def test_repeated_deletion_while_offline_rebuild_uses_latest_git_copy(store: LocalStore):
    note, data, _ = await deleted(store)
    preview = await store.preview_note_restore(note.id)
    await store.restore_note(
        note.id, RestoreNote(token=preview.token, confirmation=f"Restore {note.id}")
    )
    path = store.notes_root / note.path
    newer = data + b"\nSecond generation\n"
    path.write_bytes(newer)
    commit(store.notes_root)
    path.unlink()
    removal = commit(store.notes_root)
    # No reconciliation saw the second edit or deletion before database loss.
    async with store.session_factory() as session:
        await session.execute(sa.delete(Note))
        await session.commit()
    await store.rebuild_metadata()
    preview = await store.preview_note_restore(note.id)
    assert preview.content.encode() == newer
    assert preview.removal_commit == removal


async def test_unreadable_return_blocks_restore_and_absence(store: LocalStore, monkeypatch):
    note, data, _ = await deleted(store)
    preview = await store.preview_note_restore(note.id)
    returned = store.notes_root / "Unreadable.md"
    returned.write_bytes(data)
    os.utime(returned, (1, 1))
    original = Path.read_bytes

    def read(path):
        if path == returned:
            raise PermissionError("test unreadable return")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    for _ in range(2):
        with pytest.raises(ValidationFailed):
            await store.restore_note(
                note.id, RestoreNote(token=preview.token, confirmation=f"Restore {note.id}")
            )
    assert not (store.notes_root / note.path).exists()


async def test_interrupted_receipt_publication_does_not_poison_next_scan(
    store: LocalStore, monkeypatch
):
    from coppermind_store import note_history

    note = await store.create_note(CreateNote(title="Receipt interruption"))
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    (store.notes_root / note.path).unlink()
    commit(store.notes_root)
    original = note_history.os.link

    def interrupted(*args, **kwargs):
        raise OSError("test crash before atomic publish")

    monkeypatch.setattr(note_history.os, "link", interrupted)
    from coppermind.store_protocol import MetadataUnavailable

    with pytest.raises(MetadataUnavailable):
        await reconcile_once(store)
    assert not list(note_history.directory(store).glob("*.json"))
    monkeypatch.setattr(note_history.os, "link", original)
    await reconcile_once(store)
    assert (await store.list_notes(NoteQuery(state="deleted"))).items[0].id == note.id


async def test_reviewed_source_evidence_and_search_survive_deletion_restore(store: LocalStore):
    from coppermind_store import filing, indexer

    from coppermind.store_protocol import IngestRequest, PatchFrontmatter, SearchQuery

    request = IngestRequest.model_validate(
        {
            "source": {
                "provider": "test",
                "external_source_id": "recovery-source",
                "source_type": "transcript",
                "artifacts": [
                    {
                        "name": "transcript.txt",
                        "mime_type": "text/plain",
                        "content": "Preserve the quasar original.",
                    }
                ],
            },
            "note": {
                "title": "Quasar decision",
                "body": "Quasar decision confirmed.",
                "frontmatter": {"date": "2026-10-14", "type": "work", "context": "internal"},
            },
        }
    )
    result = await store.ingest(request)
    note = await store.get_note(result.note.id)
    await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"reviewed": True}), note.content_hash
    )
    await filing.file_reviewed(store)
    await indexer.catch_up(store)
    note = await store.get_note(note.id)
    originals = {str(p): p.read_bytes() for p in store.sources_root.rglob("transcript.txt")}
    assert originals
    assert (await store.search(SearchQuery(q="quasar"))).items
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    (store.notes_root / note.path).unlink()
    commit(store.notes_root)
    await reconcile_once(store)
    await indexer.catch_up(store)
    assert not (await store.search(SearchQuery(q="quasar"))).items
    preview = await store.preview_note_restore(note.id)
    for path in store.notes_root.rglob("*.md"):
        os.utime(path, (1, 1))
    await store.restore_note(
        note.id, RestoreNote(token=preview.token, confirmation=f"Restore {note.id}")
    )
    await filing.file_reviewed(store)
    await indexer.catch_up(store)
    assert (await store.search(SearchQuery(q="quasar"))).items
    assert {str(p): p.read_bytes() for p in store.sources_root.rglob("transcript.txt")} == originals
    assert (await store.get_status()).counters.sources == 1


async def test_two_git_generations_between_scans_never_offer_old_contents(store: LocalStore):
    note, data, _ = await deleted(store)
    path = store.notes_root / note.path
    newer = data + b"Newer return between scans\n"
    path.write_bytes(newer)
    commit(store.notes_root)
    path.unlink()
    commit(store.notes_root)
    await reconcile_once(store)
    assert (await store.preview_note_restore(note.id)).content.encode() == newer
