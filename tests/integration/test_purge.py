"""Retention removal against real Git, source bundles, and PostgreSQL."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from coppermind_store import note_history, purge
from coppermind_store.reconciler import reconcile_once

from coppermind import frontmatter as fm
from coppermind.db.models import Note, SearchDocument, Source
from coppermind.purge import AbandonPurge, ConfirmPurge
from coppermind.store_protocol import (
    CreateNote,
    NotesFilesystemUnavailable,
    PurgeConflict,
    ValidationFailed,
)
from tests.integration.test_ingest_write_protocol import sample
from tests.integration.test_note_recovery import commit, deleted, git


def retention(store, days):
    state = store.control.store.read("settings")
    values = dict(state.body)
    values["retention"] = {"days": days}
    store.control.store.write("settings", values, if_revision=state.revision)
    store.sources_root.mkdir(exist_ok=True)


async def age(store, note_id, at):
    for path in note_history.directory(store).glob(f"{note_id}-*.json"):
        receipt = note_history.Receipt.model_validate_json(path.read_bytes())
        receipt.recorded_at = at
        path.write_text(receipt.model_dump_json())
    async with store.session_factory() as session:
        await session.execute(sa.update(Note).where(Note.id == note_id).values(updated_at=at))
        await session.commit()


def confirmation(preview):
    return ConfirmPurge(token=preview.token, confirmation="Purge listed items")


async def orphan(store):
    result = await store.ingest(sample())
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    (store.notes_root / result.note.path).unlink()
    commit(store.notes_root)
    await reconcile_once(store)
    return result


async def test_retention_boundary_and_default(store):
    assert store.control.settings().retention.days == 30
    store.sources_root.mkdir(exist_ok=True)
    note, _, _ = await deleted(store)
    now = datetime.now(UTC)
    boundary = now - timedelta(days=30)
    await age(store, note.id, boundary)
    assert len(await purge.candidates(store, now)) == 1
    assert not await purge.candidates(store, now - timedelta(microseconds=1))


async def test_zero_day_exact_list_index_and_git_preserved(store):
    retention(store, 0)
    note, _, removal = await deleted(store)
    async with store.session_factory() as session:
        session.add(
            SearchDocument(
                kind="note",
                ref_id=note.id,
                path=note.path,
                title="stale",
                body="stale",
                version=note.content_hash,
                indexed_at=datetime.now(UTC),
            )
        )
        await session.commit()
    before = git(store.notes_root, "rev-parse", "HEAD")
    preview = await store.preview_purge()
    assert [c.id for c in preview.candidates] == [note.id]
    assert preview.counts.by_origin == {"Local notes": 1}
    done = await store.confirm_purge(confirmation(preview))
    assert done.candidates == preview.candidates
    assert done.state == "complete"
    assert not list(note_history.directory(store).glob(f"{note.id}-*.json"))
    async with store.session_factory() as session:
        assert await session.get(Note, note.id) is None
        assert await session.scalar(sa.select(sa.func.count()).select_from(SearchDocument)) == 0
    assert git(store.notes_root, "rev-parse", "HEAD") == before == removal
    assert git(store.notes_root, "show", f"{removal}^:{note.path}")
    await store.rebuild_metadata()
    async with store.session_factory() as session:
        assert await session.get(Note, note.id) is None
    assert (await store.confirm_purge(confirmation(preview))).state == "complete"


async def test_orphan_bundle_projection_claim_and_search_removed(store):
    retention(store, 0)
    ingested = await orphan(store)
    bundle = store.sources_root / ingested.source.id
    manifest = json.loads((bundle / "manifest.json").read_text())
    # Ingest already renders the projection, even before it is published to search.
    projection = store.notes_root / manifest["projection_path"]
    assert projection.exists()
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
    preview = await store.preview_purge()
    assert preview.counts.notes == preview.counts.sources == 1
    assert preview.counts.by_provider == {"plaud": 2}
    done = await store.confirm_purge(confirmation(preview))
    assert done.candidates == preview.candidates
    assert not bundle.exists() and not projection.exists()
    assert not list(store.sources_root.glob(".external-id-*.json"))
    async with store.session_factory() as session:
        assert await session.get(Source, ingested.source.id) is None
        assert await session.scalar(sa.select(sa.func.count()).select_from(SearchDocument)) == 0
    await store.rebuild_metadata()
    assert not (await store.preview_purge()).candidates


@pytest.mark.parametrize("mirrored", [False, True])
async def test_live_reference_protects_source_even_before_scan(store, mirrored):
    retention(store, 0)
    ingested = await orphan(store)
    live = await store.create_note(CreateNote(title="Keep evidence"))
    path = store.notes_root / live.path
    path.write_text(fm.patch(path.read_text(), {"sources": [ingested.source.id]}))
    if mirrored:
        await reconcile_once(store)
    preview = await store.preview_purge()
    assert preview.counts.sources == 0
    assert preview.counts.notes == 1
    await store.confirm_purge(confirmation(preview))
    assert (store.sources_root / ingested.source.id / "manifest.json").exists()


@pytest.mark.parametrize(
    "change", ["return", "new_reference", "settings", "new_candidate", "artifact"]
)
async def test_changed_set_or_evidence_refuses_all_removal(store, change):
    retention(store, 0)
    ingested = await orphan(store)
    preview = await store.preview_purge()
    bundle = store.sources_root / ingested.source.id
    if change == "return":
        receipt = note_history.receipts(store)[ingested.note.id]
        (store.notes_root / "Returned.md").write_bytes(receipt.data())
    elif change == "new_reference":
        note = await store.create_note(CreateNote(title="New citation"))
        path = store.notes_root / note.path
        path.write_text(fm.patch(path.read_text(), {"sources": [ingested.source.id]}))
    elif change == "settings":
        retention(store, 30)
    elif change == "artifact":
        next(bundle.glob("r*/*.txt")).write_text("Changed content")
    else:
        note = await store.create_note(CreateNote(title="Second removal"))
        commit(store.notes_root)
        (store.notes_root / note.path).unlink()
        commit(store.notes_root)
        await reconcile_once(store)
    with pytest.raises(PurgeConflict):
        await store.confirm_purge(confirmation(preview))
    assert bundle.exists()
    assert note_history.receipts(store)[ingested.note.id]


async def test_partial_failure_receipt_problem_and_retry_after_restart(store, monkeypatch):
    retention(store, 0)
    ingested = await orphan(store)
    preview = await store.preview_purge()
    real = purge._unlink

    def fail(store, item):
        if item["candidate"]["kind"] == "source":
            raise OSError("simulated disk error")
        real(store, item)

    monkeypatch.setattr(purge, "_unlink", fail)
    with pytest.raises(NotesFilesystemUnavailable):
        await store.confirm_purge(confirmation(preview))
    assert purge.read(store, preview.token)["state"] == "failed"
    assert any(p.kind == "purge" for p in await store.get_problems())
    assert not note_history.receipts(store)
    await reconcile_once(store)
    assert (await store.preview_purge()).token == preview.token
    monkeypatch.setattr(purge, "_unlink", real)
    from coppermind_store.notes import LocalStore

    restarted = LocalStore(
        store.notes_root, store.control, store.session_factory, store.sources_root
    )
    done = await restarted.confirm_purge(confirmation(preview))
    assert done.state == "complete"
    assert not (store.sources_root / ingested.source.id).exists()
    assert not any(p.kind == "purge" for p in await restarted.get_problems())


async def test_failed_purge_survives_refused_retry_and_resumes(store, monkeypatch):
    retention(store, 0)
    ingested = await orphan(store)
    preview = await store.preview_purge()
    real = purge._unlink

    def fail(store, item):
        if item["candidate"]["kind"] == "source":
            raise OSError("simulated disk error")
        real(store, item)

    monkeypatch.setattr(purge, "_unlink", fail)
    with pytest.raises(NotesFilesystemUnavailable):
        await store.confirm_purge(confirmation(preview))
    monkeypatch.setattr(purge, "_unlink", real)
    retention(store, 30)
    monkeypatch.setattr(purge, "disk_references", lambda s: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(NotesFilesystemUnavailable):
        await store.confirm_purge(confirmation(preview))
    assert purge.read(store, preview.token)["state"] == "failed"
    assert (await store.preview_purge()).token == preview.token
    monkeypatch.undo()
    done = await store.confirm_purge(confirmation(preview))
    assert done.state == "complete"
    assert not (store.sources_root / ingested.source.id).exists()


async def test_repeated_previews_keep_one_unconfirmed_receipt(store):
    retention(store, 0)
    await deleted(store)
    first = await store.preview_purge()
    second = await store.preview_purge()
    assert first.token != second.token
    assert [r["token"] for r in purge.records(store)] == [second.token]
    with pytest.raises(ValidationFailed):
        await store.confirm_purge(confirmation(first))


async def test_abandon_releases_failed_purge_and_reports_outcome(store, monkeypatch):
    retention(store, 0)
    ingested = await orphan(store)
    preview = await store.preview_purge()
    real = purge._unlink

    def fail(store, item):
        raise OSError("simulated disk error")

    monkeypatch.setattr(purge, "_unlink", fail)
    with pytest.raises(NotesFilesystemUnavailable):
        await store.confirm_purge(confirmation(preview))
    assert purge.pending_ids(store, "source") == {ingested.source.id}
    with pytest.raises(ValidationFailed):
        await store.abandon_purge(AbandonPurge(token="0" * 64))

    abandoned = await store.abandon_purge(AbandonPurge(token=preview.token))
    assert abandoned.state == "abandoned"
    assert sorted(abandoned.remaining) == sorted([ingested.note.id, ingested.source.id])
    assert abandoned.completed == 0
    assert purge.pending_ids(store, "source") == set()
    assert purge.read(store, preview.token)["state"] == "abandoned"
    problems = [p for p in await store.get_problems() if p.kind == "purge"]
    assert len(problems) == 1 and "2 were not" in problems[0].reason
    assert (store.sources_root / ingested.source.id).exists()

    monkeypatch.setattr(purge, "_unlink", real)
    with pytest.raises(ValidationFailed):
        await store.abandon_purge(AbandonPurge(token=preview.token))
    fresh = await store.preview_purge()
    assert fresh.token != preview.token and fresh.state == "preview"
    assert {c.id for c in fresh.candidates} == {ingested.note.id, ingested.source.id}
    done = await store.confirm_purge(confirmation(fresh))
    assert done.state == "complete"
    assert not [p for p in await store.get_problems() if p.kind == "purge"]


async def test_abandon_after_commit_reports_everything_removed(store, monkeypatch):
    retention(store, 0)
    ingested = await orphan(store)
    preview = await store.preview_purge()
    real = purge.save

    def crash_before_complete(store, record):
        if record["state"] == "complete":
            raise OSError("state volume lost")
        real(store, record)

    monkeypatch.setattr(purge, "save", crash_before_complete)
    with pytest.raises(NotesFilesystemUnavailable):
        await store.confirm_purge(confirmation(preview))
    monkeypatch.setattr(purge, "save", real)
    assert purge.read(store, preview.token)["state"] == "failed"
    abandoned = await store.abandon_purge(AbandonPurge(token=preview.token))
    assert abandoned.state == "abandoned" and abandoned.remaining == []
    assert abandoned.completed == 2
    assert not (store.sources_root / ingested.source.id).exists()
    assert not [p for p in await store.get_problems() if p.kind == "purge"]


async def test_corrupt_receipt_does_not_halt_reconcile_and_is_reported(store):
    retention(store, 0)
    note, _, _ = await deleted(store)
    purge.directory(store).mkdir(parents=True, exist_ok=True)
    (purge.directory(store) / ("f" * 64 + ".json")).write_text("{broken")
    await reconcile_once(store)
    assert purge.pending_ids(store, "note") == set()
    problems = [p for p in await store.get_problems() if p.kind == "purge"]
    assert len(problems) == 1 and "unreadable" in problems[0].reason
    assert note_history.receipts(store)[note.id]


async def test_terminal_receipts_are_not_read_by_reconcile_or_problems(store):
    retention(store, 0)
    await deleted(store)
    preview = await store.preview_purge()
    done = await store.confirm_purge(confirmation(preview))
    assert done.state == "complete"
    assert list(purge.directory(store).glob("*.json")) == []
    assert purge.read(store, preview.token)["state"] == "complete"
    for path in (purge.directory(store) / "done").glob("*.json"):
        path.write_text("{broken")
    await reconcile_once(store)
    assert not [p for p in await store.get_problems() if p.kind == "purge"]


async def test_unparseable_file_blocks_source_removal(store):
    retention(store, 0)
    await orphan(store)
    (store.notes_root / "Unparsed.md").write_text("---\nsources: [broken\n---\n")
    with pytest.raises(NotesFilesystemUnavailable):
        await store.preview_purge()


async def test_unreadable_receipt_is_visible_and_refuses_purge(store):
    retention(store, 0)
    note, _, _ = await deleted(store)
    preview = await store.preview_purge()
    (purge.directory(store) / (preview.token + ".json")).write_text("broken")
    with pytest.raises(NotesFilesystemUnavailable):
        await store.preview_purge()
    assert any(p.kind == "purge" and "unreadable" in p.reason for p in await store.get_problems())
    assert note_history.receipts(store)[note.id]
