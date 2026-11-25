"""A deletion answers once: records go at once, Git history stays, nothing waits for a clean-up."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from coppermind_store import artifacts, filing, note_history
from coppermind_store.notes import LocalStore
from coppermind_store.reconciler import reconcile_once
from coppermind_store.removal import finish_upgrade_removals

from coppermind import frontmatter as fm
from coppermind.db.models import Artifact, Note, NoteSource, SearchDocument, Source
from coppermind.entities import EntityQuery
from coppermind.store_protocol import (
    CreateNote,
    IngestRequest,
    NoteQuery,
    PatchFrontmatter,
    SearchQuery,
    ValidationFailed,
)
from tests.integration.test_artifacts import upload
from tests.integration.test_ingest_write_protocol import sample


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


async def committed(store: LocalStore, **fields):
    """A note whose current bytes Git has recorded."""
    note = await store.create_note(CreateNote(**({"title": "Recovery"} | fields)))
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    return note


async def deleted(store: LocalStore):
    """A note deleted on a device, seen missing by one pass, then removed by Git's record."""
    note = await committed(store, body="Original evidence")
    path = store.notes_root / note.path
    data = path.read_bytes()
    path.unlink()
    await reconcile_once(store)
    assert (await store.list_notes(NoteQuery(state="missing"))).items[0].id == note.id
    removal = commit(store.notes_root)
    counts = await reconcile_once(store)
    assert counts["deleted"] == 1
    return note, data, removal


async def rows(store: LocalStore, model, *where):
    async with store.session_factory() as session:
        return list((await session.scalars(sa.select(model).where(*where))).all())


async def test_a_note_deleted_in_the_vault_is_gone_once_git_records_it(store: LocalStore):
    note, data, removal = await deleted(store)
    async with store.session_factory() as session:
        assert await session.get(Note, note.id) is None
        assert await session.scalar(sa.select(sa.func.count()).select_from(SearchDocument)) == 0
    assert not (await store.list_notes(NoteQuery())).items
    state = store.control.store.state_dir
    # No recovery copy, no receipts, nothing to clean up afterwards.
    assert not (state / "note-recovery").exists()
    assert not (state / "admin-removed").exists()
    assert not (state / "purges").exists()
    # Git history is the safety net and is left alone on purpose.
    assert git(store.notes_root, "rev-parse", "HEAD") == removal
    assert git(store.notes_root, "show", f"{removal}^:{note.path}").encode() == data.rstrip()
    # A rebuilt mirror does not bring it back either.
    await store.rebuild_metadata()
    assert not (await store.list_notes(NoteQuery())).items


async def test_a_note_git_has_not_recorded_yet_is_only_missing(store: LocalStore):
    note = await committed(store, title="Unsaved snapshot")
    path = store.notes_root / note.path
    path.write_bytes(path.read_bytes() + b"Uncaptured edit\n")
    await reconcile_once(store)
    path.unlink()
    commit(store.notes_root)
    counts = await reconcile_once(store)
    assert counts["deleted"] == 0
    assert (await store.list_notes(NoteQuery(state="missing"))).items[0].id == note.id


async def test_a_move_committed_as_delete_and_add_is_not_a_deletion(store: LocalStore):
    note = await committed(store, title="Moved")
    (store.notes_root / note.path).rename(store.notes_root / "Moved.md")
    commit(store.notes_root)
    counts = await reconcile_once(store)
    assert counts["deleted"] == 0
    assert (await store.get_note(note.id)).path == "Moved.md"


async def test_unreadable_git_never_removes_a_note(store: LocalStore):
    note = await committed(store, title="Git offline")
    (store.notes_root / note.path).unlink()
    commit(store.notes_root)
    (store.notes_root / ".git").rename(store.notes_root / ".git-offline")
    counts = await reconcile_once(store)
    assert counts["deleted"] == 0
    assert (await store.list_notes(NoteQuery(state="missing"))).items[0].id == note.id
    assert not (await store.get_note_history_status()).available


async def test_a_file_that_comes_back_is_adopted_again(store: LocalStore):
    note, data, _ = await deleted(store)
    (store.notes_root / "Elsewhere.md").write_bytes(data)
    await reconcile_once(store)
    assert (await store.get_note(note.id)).path == "Elsewhere.md"


async def test_removal_takes_the_filing_journal_with_it(store: LocalStore):
    note = await store.create_note(
        CreateNote(
            title="Filed",
            frontmatter={"date": "2026-09-01", "type": "reference", "reviewed": True},
        )
    )
    git(store.notes_root, "init", "-q")
    assert (await filing.file_reviewed(store))["filed"] == 1
    journal = store.control.store.state_dir / "filing" / f"{note.id}.json"
    assert journal.exists()
    commit(store.notes_root)
    (store.notes_root / (await store.get_note(note.id)).path).unlink()
    await reconcile_once(store)
    commit(store.notes_root)
    assert (await reconcile_once(store))["deleted"] == 1
    assert not journal.exists()


async def orphan(store: LocalStore):
    result = await store.ingest(sample())
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    (store.notes_root / result.note.path).unlink()
    commit(store.notes_root)
    return result


async def test_a_deleted_note_takes_its_orphaned_source_with_it(store: LocalStore):
    ingested = await orphan(store)
    bundle = store.sources_root / ingested.source.id
    manifest = json.loads((bundle / "manifest.json").read_text())
    projection = store.notes_root / manifest["projection_path"]
    assert projection.exists() and any(store.sources_root.glob(".external-id-*.json"))
    async with store.session_factory() as session:
        session.add(
            SearchDocument(
                kind="source",
                ref_id=ingested.source.id,
                path=manifest["projection_path"],
                title="stale",
                body="stale",
                version="r1",
                indexed_at=datetime.now(UTC),
            )
        )
        await session.commit()
    assert (await reconcile_once(store))["deleted"] == 1
    assert not bundle.exists() and not projection.exists()
    assert not list(store.sources_root.glob(".external-id-*.json"))
    async with store.session_factory() as session:
        assert await session.get(Source, ingested.source.id) is None
        assert await session.get(Note, ingested.note.id) is None
        assert await session.scalar(sa.select(sa.func.count()).select_from(SearchDocument)) == 0
    # Nothing is left for a later pass, a rebuild or an owner to finish.
    await store.rebuild_metadata()
    assert not (await store.list_notes(NoteQuery())).items


@pytest.mark.parametrize("mirrored", [False, True])
async def test_a_source_a_live_note_cites_is_kept(store: LocalStore, mirrored):
    ingested = await orphan(store)
    live = await store.create_note(CreateNote(title="Keep evidence"))
    path = store.notes_root / live.path
    path.write_text(fm.patch(path.read_text(), {"sources": [ingested.source.id]}))
    if mirrored:
        os.utime(path, (1, 1))
        await reconcile_once(store)
    await reconcile_once(store)
    assert (store.sources_root / ingested.source.id / "manifest.json").exists()
    async with store.session_factory() as session:
        assert await session.get(Note, ingested.note.id) is None
        assert await session.get(Source, ingested.source.id) is not None


async def test_a_failed_removal_is_finished_by_the_next_pass(store: LocalStore, monkeypatch):
    from coppermind_store import removal

    ingested = await orphan(store)
    bundle = store.sources_root / ingested.source.id
    original = removal._unlink

    def failing(paths):
        raise OSError("simulated failure while removing files")

    monkeypatch.setattr(removal, "_unlink", failing)
    assert (await reconcile_once(store))["deleted"] == 0
    # The row stays, so the note is still found absent and is tried again.
    assert [n.id for n in await rows(store, Note)] == [ingested.note.id]
    monkeypatch.setattr(removal, "_unlink", original)
    assert (await reconcile_once(store))["deleted"] == 1
    assert not bundle.exists()
    assert not await rows(store, Note) and not await rows(store, Source)


async def test_a_missing_note_with_a_saved_filing_copy_can_still_be_restored(store: LocalStore):
    note = await store.create_note(
        CreateNote(
            title="Filed",
            frontmatter={"date": "2026-09-01", "type": "reference", "reviewed": True},
        )
    )
    git(store.notes_root, "init", "-q")
    assert (await filing.file_reviewed(store))["filed"] == 1
    filed = (await store.get_note(note.id)).path
    (store.notes_root / filed).unlink()
    await reconcile_once(store)
    assert (await store.list_notes(NoteQuery(state="missing"))).items[0].id == note.id
    preview = await store.preview_note_restore(note.id)
    assert preview.path == filed and preview.content
    # Only a missing note has anything to restore; the route reads no recovery copy.
    live = await store.create_note(CreateNote(title="Live"))
    with pytest.raises(ValidationFailed):
        await store.preview_note_restore(live.id)


async def test_a_reviewed_note_and_its_search_entry_go_with_it(store: LocalStore):
    from coppermind_store import indexer

    result = await store.ingest(
        IngestRequest.model_validate(
            {
                "source": {
                    "provider": "test",
                    "external_source_id": "delete-once-source",
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
    )
    note = await store.get_note(result.note.id)
    await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"reviewed": True}), note.content_hash
    )
    await filing.file_reviewed(store)
    await indexer.catch_up(store)
    note = await store.get_note(note.id)
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    (store.notes_root / note.path).unlink()
    commit(store.notes_root)
    await reconcile_once(store)
    await indexer.catch_up(store)
    assert not (await store.search(SearchQuery(q="quasar"))).items
    assert not await rows(store, Note) and not await rows(store, NoteSource)
    assert not list(store.sources_root.rglob("transcript.txt"))
    assert (await store.get_status()).counters.sources == 0


async def test_the_old_recovery_machinery_is_gone(store: LocalStore):
    for name in ("directory", "save", "receipts", "recover_rows", "remember_return", "Receipt"):
        assert not hasattr(note_history, name)
    for name in ("preview_purge", "confirm_purge", "abandon_purge"):
        assert not hasattr(store, name)
    assert "retention" not in store.control.settings().model_dump()


async def test_an_upgrade_leftover_list_is_scrubbed_once_at_start(store: LocalStore):
    pending = store.control.store.state_dir / "removed-notes-pending.json"
    pending.write_text(json.dumps({"note_ids": ["01JZZZZZZZZZZZZZZZZZZZZZZZ"]}))
    assert await finish_upgrade_removals(store) == 1
    assert not pending.exists()
    assert await finish_upgrade_removals(store) == 0


async def test_a_removed_artifact_takes_its_archive_with_it(store: LocalStore):
    old = await store.create_artifact(upload("old.txt", b"old bytes\n"))
    note = await store.create_note(
        CreateNote(
            title="Cites the old one",
            frontmatter={"type": "reference", "context": "internal", "artifacts": [old.id]},
        )
    )
    live = await store.create_artifact(upload("live.txt", b"live bytes\n"))
    assert (store.artifacts_root / old.id / "r0001" / "old.txt").exists()
    await store.remove_artifact(old.id)
    assert not (store.artifacts_root / old.id).exists()
    assert not list((store.control.store.state_dir / "artifacts").glob(f"{old.id}.json"))
    assert (store.artifacts_root / live.id / "r0001" / "live.txt").exists()
    async with store.session_factory() as session:
        assert await session.get(Artifact, old.id) is None
    # A note that cited it keeps the citation it wrote.
    assert (await store.get_note(note.id)).frontmatter["artifacts"] == [old.id]


async def test_deleting_an_artifact_in_obsidian_or_through_the_api_leaves_no_archive(
    store: LocalStore,
):
    obsidian = await store.create_artifact(upload("deck.txt", b"deleted in obsidian\n"))
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    (store.notes_root / obsidian.path).unlink()
    commit(store.notes_root)
    assert (await artifacts.sweep(store))["artifacts_removed"] == 1
    agent = await store.create_artifact(upload("agent.txt", b"removed by an agent\n"))
    await store.remove_artifact(agent.id, "api")
    assert not (store.artifacts_root / obsidian.id).exists()
    assert not (store.artifacts_root / agent.id).exists()


async def test_an_absent_copy_without_a_committed_deletion_keeps_its_archive(store: LocalStore):
    """Not synced yet is not deleted: the archive stays until Git records the deletion."""
    absent = await store.create_artifact(upload("device.txt", b"gone from a device\n"))
    (store.notes_root / absent.path).unlink()
    assert (await artifacts.sweep(store))["artifacts_missing"] == 1
    assert (store.artifacts_root / absent.id / "r0001" / "device.txt").exists()


async def test_an_archive_an_earlier_release_left_marked_removed_is_swept_at_start(
    store: LocalStore,
):
    gone = await store.create_artifact(upload("gone.txt", b"gone\n"))
    manifest = artifacts.manifest_path(store, gone.id)
    document = json.loads(manifest.read_text())
    await store.remove_artifact(gone.id)
    # What the earlier release left: the archive kept, marked, with no row.
    manifest.parent.mkdir(parents=True)
    (manifest.parent / "r0001").mkdir()
    (manifest.parent / "r0001" / "gone.txt").write_bytes(b"gone\n")
    manifest.write_text(
        json.dumps(
            document
            | {"removed_at": datetime.now(UTC).isoformat(), "removed_reason": "removed in Admin"}
        )
    )
    kept = await store.create_artifact(upload("kept.txt", b"kept\n"))
    assert await artifacts.sweep_removed_archives(store) == 1
    assert not manifest.parent.exists()
    assert (store.artifacts_root / kept.id / "r0001" / "kept.txt").exists()


async def test_audio_removed_after_review_keeps_its_manifest_as_the_receipt(store: LocalStore):
    audio = await store.create_artifact(upload("memo.txt", b"recorded words\n"))
    manifest = json.loads(artifacts.manifest_path(store, audio.id).read_text())
    await artifacts.remove_recording_audio(store, audio.id, manifest["current_revision"])
    receipt = json.loads(artifacts.manifest_path(store, audio.id).read_text())
    assert receipt["removed_reason"] == artifacts.REMOVED_AFTER_REVIEW
    assert not (store.artifacts_root / audio.id / "r0001" / "memo.txt").exists()


async def test_one_unreadable_note_elsewhere_does_not_block_a_removal(store: LocalStore):
    ingested = await orphan(store)
    broken = store.notes_root / "Broken.md"
    broken.write_text("---\ntitle: [unclosed\n---\nBody\n")
    keeper = await store.create_note(CreateNote(title="Keeper"))
    assert keeper
    assert (await reconcile_once(store))["deleted"] == 1
    assert not (store.sources_root / ingested.source.id).exists()


async def test_a_source_a_note_that_cannot_be_parsed_names_is_kept(store: LocalStore):
    ingested = await orphan(store)
    (store.notes_root / "Broken.md").write_text(
        f"---\ntitle: [unclosed\n---\nSee {ingested.source.id}\n"
    )
    await reconcile_once(store)
    assert (store.sources_root / ingested.source.id / "manifest.json").exists()


async def test_a_person_only_a_deleted_note_named_can_come_back(store: LocalStore):
    from tests.integration.test_entities import reviewed_note

    note = await reviewed_note(store)
    await store.entities.refresh()
    assert (await store.entities.list(EntityQuery()))["items"]
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    (store.notes_root / note.path).unlink()
    commit(store.notes_root)
    assert (await reconcile_once(store))["deleted"] == 1
    assert not store.entities.load().suppressed


async def test_a_removal_that_stops_part_way_keeps_the_manifest_for_the_retry(
    store: LocalStore, monkeypatch
):
    from coppermind_store import removal

    ingested = await orphan(store)
    bundle = store.sources_root / ingested.source.id
    original = removal._unlink
    seen: list[str] = []

    def stop_before_the_manifest(paths):
        paths = list(paths)
        names = [path.name for path in paths]
        if "manifest.json" in names:
            seen.extend(names)
            raise OSError("stopped part way")
        original(paths)

    monkeypatch.setattr(removal, "_unlink", stop_before_the_manifest)
    await reconcile_once(store)
    # Every other file of the bundle went first; the manifest is the last of its files.
    assert seen.index("manifest.json") == len(seen) - 2
    assert (bundle / "manifest.json").exists()
    monkeypatch.setattr(removal, "_unlink", original)
    assert (await reconcile_once(store))["deleted"] == 1
    assert not bundle.exists()


async def test_a_source_cited_while_its_note_is_being_removed_is_kept(
    store: LocalStore, monkeypatch
):
    from coppermind_store import removal

    ingested = await orphan(store)

    async def cited(*args):
        return True

    monkeypatch.setattr(removal, "_cited_now", cited)
    assert (await reconcile_once(store))["deleted"] == 1
    assert (store.sources_root / ingested.source.id / "manifest.json").exists()
    assert await rows(store, Source, Source.id == ingested.source.id)
