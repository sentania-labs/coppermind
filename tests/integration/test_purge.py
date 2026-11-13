"""Retention removal against real Git, source bundles, and PostgreSQL."""

import json
import shutil
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from coppermind_store import filing, indexer, note_history, purge
from coppermind_store.reconciler import reconcile_once
from coppermind_store.sources import _external_id_claim_path

from coppermind import frontmatter as fm
from coppermind.db.models import Note, SearchDocument, Source
from coppermind.purge import AbandonPurge, ConfirmPurge, PurgeBreakdown
from coppermind.store_protocol import (
    CreateNote,
    MetadataUnavailable,
    NotesFilesystemUnavailable,
    PurgeConflict,
    SourcesFilesystemUnavailable,
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
    assert len((await purge.candidates(store, now))[0]) == 1
    items, kept = await purge.candidates(store, now - timedelta(microseconds=1))
    assert not items and kept["notes_within_retention"] == 1


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
    assert preview.counts.by_origin == {"Local notes": PurgeBreakdown(notes=1)}
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


async def test_purge_removes_the_filing_journal_with_the_note(store):
    retention(store, 0)
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
    preview = await store.preview_purge()
    assert [c.id for c in preview.candidates] == [note.id]
    assert (await store.confirm_purge(confirmation(preview))).state == "complete"
    assert not journal.exists()


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
    assert preview.counts.by_provider == {"plaud": PurgeBreakdown(notes=1, sources=1)}
    done = await store.confirm_purge(confirmation(preview))
    assert done.candidates == preview.candidates
    assert not bundle.exists() and not projection.exists()
    assert not list(store.sources_root.glob(".external-id-*.json"))
    async with store.session_factory() as session:
        assert await session.get(Source, ingested.source.id) is None
        assert await session.scalar(sa.select(sa.func.count()).select_from(SearchDocument)) == 0
    await store.rebuild_metadata()
    assert not (await store.preview_purge()).candidates


@pytest.mark.parametrize("keep_recorded", [False, True])
async def test_discovered_generated_pages_are_bound_to_preview_and_removed(store, keep_recorded):
    retention(store, 0)
    ingested = await orphan(store)
    bundle = store.sources_root / ingested.source.id
    manifest = json.loads((bundle / "manifest.json").read_text())
    recorded = store.notes_root / manifest["projection_path"]
    moved = store.notes_root / "_Sources" / "Relocated.md"
    moved.write_bytes(recorded.read_bytes())
    if not keep_recorded:
        recorded.unlink()
    preview = await store.preview_purge()
    extra = moved.with_name("Another generated page.md")
    extra.write_bytes(moved.read_bytes())
    with pytest.raises(PurgeConflict):
        await store.confirm_purge(confirmation(preview))
    assert moved.exists() and extra.exists() and bundle.exists()

    preview = await store.preview_purge()
    await store.confirm_purge(confirmation(preview))
    assert not any(path.exists() for path in (recorded, moved, extra, bundle))
    assert (await indexer.rebuild(store))["sources"] == 0
    async with store.session_factory() as session:
        assert await session.get(Source, ingested.source.id) is None
        assert await session.get(SearchDocument, ("source", ingested.source.id)) is None


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
    assert any(p.kind == "purge_unfinished" for p in await store.get_problems())
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
    assert not any(p.kind.startswith("purge") for p in await restarted.get_problems())


async def test_confirm_failing_before_removal_says_to_preview_again(store, monkeypatch):
    retention(store, 0)
    ingested = await orphan(store)
    preview = await store.preview_purge()
    real = purge.disk_references
    calls = []

    def fail_in_transaction(store):
        calls.append(1)
        if len(calls) > 1:
            raise OSError("simulated disk error")
        return real(store)

    monkeypatch.setattr(purge, "disk_references", fail_in_transaction)
    with pytest.raises(NotesFilesystemUnavailable):
        await store.confirm_purge(confirmation(preview))
    assert len(calls) > 1
    assert purge.read(store, preview.token)["state"] == "preview"
    assert (store.sources_root / ingested.source.id).exists()
    problems = [p for p in await store.get_problems() if p.kind.startswith("purge")]
    assert [p.reference for p in problems] == [preview.token]
    assert "try again" in problems[0].reason
    assert "abandon" not in problems[0].reason
    monkeypatch.setattr(purge, "disk_references", real)
    fresh = await store.preview_purge()
    assert fresh.token != preview.token
    assert not [p for p in await store.get_problems() if p.kind.startswith("purge")]


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
    problems = [p for p in await store.get_problems() if p.kind.startswith("purge")]
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
    assert not [p for p in await store.get_problems() if p.kind.startswith("purge")]


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
    assert not [p for p in await store.get_problems() if p.kind.startswith("purge")]


async def test_abandoned_completed_note_proves_surviving_source_can_be_purged(store, monkeypatch):
    retention(store, 0)
    ingested = await orphan(store)
    live = await store.create_note(CreateNote(title="Last live citation"))
    live_path = store.notes_root / live.path
    live_path.write_text(fm.patch(live_path.read_text(), {"sources": [ingested.source.id]}))
    commit(store.notes_root)
    await reconcile_once(store)
    preview = await store.preview_purge()
    assert [c.id for c in preview.candidates] == [ingested.note.id]
    real = purge.save

    def fail_complete(store, record):
        if record["state"] == "complete":
            raise OSError("state volume lost after database commit")
        real(store, record)

    with monkeypatch.context() as patch:
        patch.setattr(purge, "save", fail_complete)
        with pytest.raises(NotesFilesystemUnavailable):
            await store.confirm_purge(confirmation(preview))
    abandoned = await store.abandon_purge(AbandonPurge(token=preview.token))
    assert abandoned.completed == 1 and abandoned.remaining == []
    assert purge.read(store, preview.token)["completed"] == [ingested.note.id]
    assert not (await store.preview_purge()).candidates

    live_path.unlink()
    commit(store.notes_root)
    await reconcile_once(store)
    preview = await store.preview_purge()
    assert {c.id for c in preview.candidates} == {live.id, ingested.source.id}
    await store.confirm_purge(confirmation(preview))
    assert not (store.sources_root / ingested.source.id).exists()
    async with store.session_factory() as session:
        assert await session.get(Source, ingested.source.id) is None


async def test_corrupt_receipt_does_not_halt_reconcile_and_is_reported(store):
    retention(store, 0)
    note, _, _ = await deleted(store)
    purge.directory(store).mkdir(parents=True, exist_ok=True)
    (purge.directory(store) / ("f" * 64 + ".json")).write_text("{broken")
    await reconcile_once(store)
    assert purge.pending_ids(store, "note") == set()
    problems = [p for p in await store.get_problems() if p.kind == "purge_receipt"]
    assert [p.reference for p in problems] == ["f" * 64]
    assert "unreadable" in problems[0].reason
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
    await purge.scan_problems(store)
    assert [p.reference for p in await store.get_problems() if p.kind == "purge_receipt"] == [
        preview.token
    ]


async def test_unparseable_file_blocks_source_removal(store):
    retention(store, 0)
    await orphan(store)
    (store.notes_root / "Unparsed.md").write_text("---\nsources: [broken\n---\n")
    with pytest.raises(NotesFilesystemUnavailable):
        await store.preview_purge()


async def test_unreadable_receipt_is_skipped_reported_and_quarantined_by_abandon(store):
    retention(store, 0)
    note, _, _ = await deleted(store)
    first = await store.preview_purge()
    path = purge.directory(store) / (first.token + ".json")
    path.write_text("broken")
    second = await store.preview_purge()
    assert second.token != first.token and [c.id for c in second.candidates] == [note.id]
    problems = await store.get_problems()
    assert [p.reference for p in problems if p.kind == "purge_receipt"] == [first.token]
    with pytest.raises(ValidationFailed):
        await store.confirm_purge(confirmation(AbandonPurge(token=first.token)))
    done = await store.confirm_purge(confirmation(second))
    assert done.state == "complete"

    result = await store.abandon_purge(AbandonPurge(token=first.token))
    assert result.state == "abandoned" and result.candidates == []
    assert not path.exists()
    assert (purge.directory(store) / "quarantine" / path.name).read_text() == "broken"
    assert not [p for p in await store.get_problems() if p.kind.startswith("purge")]


async def test_stuck_applying_receipt_can_be_abandoned(store):
    retention(store, 0)
    await deleted(store)
    preview = await store.preview_purge()
    record = purge.read(store, preview.token)
    record.update(state="applying")
    purge.save(store, record)
    assert purge.pending_ids(store, "note")
    result = await store.abandon_purge(AbandonPurge(token=preview.token))
    assert result.state == "abandoned" and purge.pending_ids(store, "note") == set()
    with pytest.raises(ValidationFailed):
        await store.confirm_purge(confirmation(preview))
    assert purge.read(store, preview.token)["state"] == "abandoned"
    async with store.session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 1
    assert (await store.preview_purge()).state == "preview"


async def test_leftover_rows_after_interrupted_purge_are_listed_and_removed(store):
    retention(store, 0)
    ingested = await orphan(store)
    bundle = store.sources_root / ingested.source.id
    projection = (
        store.notes_root / json.loads((bundle / "manifest.json").read_text())["projection_path"]
    )
    assert projection.exists()
    async with store.session_factory() as session:
        row = await session.get(Source, ingested.source.id)
    assert row is not None
    claim = _external_id_claim_path(store.sources_root, row.provider, row.external_source_id)
    shutil.rmtree(bundle)
    claim.unlink()
    for path in note_history.directory(store).glob(f"{ingested.note.id}-*.json"):
        path.unlink()
    preview = await store.preview_purge()
    assert {(c.id, c.leftover) for c in preview.candidates} == {
        (ingested.note.id, True),
        (ingested.source.id, True),
    }
    done = await store.confirm_purge(confirmation(preview))
    assert done.state == "complete"
    assert not bundle.exists() and not projection.exists()
    async with store.session_factory() as session:
        assert await session.get(Note, ingested.note.id) is None
        assert await session.get(Source, ingested.source.id) is None
    assert (await store.preview_purge()).candidates == []


async def test_damaged_bundle_is_not_a_leftover_and_is_reported(store):
    retention(store, 0)
    ingested = await orphan(store)
    bundle = store.sources_root / ingested.source.id
    (bundle / "manifest.json").unlink()
    assert any(p.is_file() for p in bundle.rglob("*"))
    preview = await store.preview_purge()
    assert ingested.source.id not in {c.id for c in preview.candidates}
    damaged = [p for p in await store.get_problems() if p.kind == "damaged_source"]
    assert [p.source_id for p in damaged] == [ingested.source.id]
    await store.confirm_purge(confirmation(preview))
    assert bundle.exists()
    async with store.session_factory() as session:
        assert await session.get(Source, ingested.source.id) is not None


async def test_empty_bundle_with_live_opening_note_is_not_a_leftover(store):
    retention(store, 0)
    ingested = await store.ingest(sample())
    shutil.rmtree(store.sources_root / ingested.source.id)
    preview = await store.preview_purge()
    assert preview.candidates == []


def partial_source_failure(real):
    """Remove the manifest as a real interrupted unlink would, then fail."""

    def unlink(store, item):
        if item["candidate"]["kind"] == "source":
            (store.sources_root / item["candidate"]["id"] / "manifest.json").unlink()
            raise OSError("simulated disk error")
        real(store, item)

    return unlink


async def test_database_commits_before_any_file_is_removed(store, monkeypatch):
    retention(store, 0)
    ingested = await orphan(store)
    preview = await store.preview_purge()
    bundle = store.sources_root / ingested.source.id
    recovery = sorted(note_history.directory(store).glob(f"{ingested.note.id}-*.json"))
    assert recovery

    async def refuse(session, source_id, now):
        raise sa.exc.OperationalError("DELETE", {}, Exception("database went away"))

    monkeypatch.setattr(indexer, "drop_source", refuse)
    with pytest.raises(MetadataUnavailable):
        await store.confirm_purge(confirmation(preview))
    assert purge.read(store, preview.token)["state"] == "preview"
    assert bundle.exists() and (bundle / "manifest.json").exists()
    assert sorted(note_history.directory(store).glob(f"{ingested.note.id}-*.json")) == recovery
    async with store.session_factory() as session:
        assert await session.get(Note, ingested.note.id) is not None
        assert await session.get(Source, ingested.source.id) is not None

    monkeypatch.undo()
    assert (await store.confirm_purge(confirmation(preview))).state == "complete"
    assert not bundle.exists()


async def test_mid_flight_failure_is_consistent_truthfully_reported_and_finishable(
    store, monkeypatch
):
    retention(store, 0)
    ingested = await orphan(store)
    preview = await store.preview_purge()
    bundle = store.sources_root / ingested.source.id
    manifest = json.loads((bundle / "manifest.json").read_text())
    projection = store.notes_root / manifest["projection_path"]
    monkeypatch.setattr(purge, "_unlink", partial_source_failure(purge._unlink))
    with pytest.raises(NotesFilesystemUnavailable):
        await store.confirm_purge(confirmation(preview))
    monkeypatch.undo()

    assert purge.read(store, preview.token)["state"] == "failed"
    async with store.session_factory() as session:
        assert await session.get(Note, ingested.note.id) is None
        assert await session.get(Source, ingested.source.id) is None
    assert not list(note_history.directory(store).glob(f"{ingested.note.id}-*.json"))
    assert bundle.exists() and not (bundle / "manifest.json").exists()

    await reconcile_once(store)
    async with store.session_factory() as session:
        assert await session.get(Note, ingested.note.id) is None
    assert not list(note_history.directory(store).glob(f"{ingested.note.id}-*.json"))
    problems = await store.get_problems()
    assert [p.kind for p in problems if p.kind.startswith("purge")] == ["purge_unfinished"]
    assert not [p for p in problems if p.kind == "damaged_source"]
    assert (await store.get_status()).counters.purge_problems == 1

    abandoned = await store.abandon_purge(AbandonPurge(token=preview.token))
    assert abandoned.completed == 1 and abandoned.remaining == [ingested.source.id]
    await reconcile_once(store)
    async with store.session_factory() as session:
        assert await session.get(Note, ingested.note.id) is None
        assert await session.get(Source, ingested.source.id) is None
    problems = await store.get_problems()
    assert not [p for p in problems if p.kind == "damaged_source"]
    (remaining,) = [p for p in problems if p.kind == "purge"]
    assert "1 listed items were removed. 1 were not" in remaining.reason
    assert (await store.get_status()).counters.purge_problems == 1

    fresh = await store.preview_purge()
    assert [(c.id, c.leftover) for c in fresh.candidates] == [(ingested.source.id, True)]
    done = await store.confirm_purge(confirmation(fresh))
    assert done.state == "complete"
    assert not bundle.exists() and not projection.exists()
    assert not list(store.sources_root.glob(".external-id-*.json"))
    assert not [p for p in await store.get_problems() if p.kind.startswith("purge")]
    assert (await store.get_status()).counters.purge_problems == 0


async def test_residual_files_are_finished_only_while_unchanged_and_unreferenced(
    store, monkeypatch
):
    retention(store, 0)
    ingested = await orphan(store)
    preview = await store.preview_purge()
    bundle = store.sources_root / ingested.source.id
    monkeypatch.setattr(purge, "_unlink", partial_source_failure(purge._unlink))
    with pytest.raises(NotesFilesystemUnavailable):
        await store.confirm_purge(confirmation(preview))
    monkeypatch.undo()
    await store.abandon_purge(AbandonPurge(token=preview.token))

    stray = bundle / "stray.txt"
    stray.write_text("not part of the saved list")
    assert not (await store.preview_purge()).candidates
    stray.unlink()
    assert [c.id for c in (await store.preview_purge()).candidates] == [ingested.source.id]

    live = await store.create_note(CreateNote(title="Cites the leftover"))
    live_path = store.notes_root / live.path
    live_path.write_text(fm.patch(live_path.read_text(), {"sources": [ingested.source.id]}))
    commit(store.notes_root)
    await reconcile_once(store)
    assert not (await store.preview_purge()).candidates
    assert bundle.exists()


async def test_file_failures_name_the_volume_that_failed(store, monkeypatch):
    retention(store, 0)
    ingested = await orphan(store)
    preview = await store.preview_purge()
    bundle = store.sources_root / ingested.source.id

    def fail(store, item):
        if item["candidate"]["kind"] == "source":
            raise OSError(5, "I/O error", str(bundle / "manifest.json"))

    monkeypatch.setattr(purge, "_unlink", fail)
    with pytest.raises(SourcesFilesystemUnavailable):
        await store.confirm_purge(confirmation(preview))


async def test_status_count_agrees_with_every_kind_of_purge_problem(store):
    retention(store, 0)
    ingested = await orphan(store)
    (store.sources_root / ingested.source.id / "manifest.json").unlink()
    purge.directory(store).mkdir(parents=True, exist_ok=True)
    (purge.directory(store) / ("e" * 64 + ".json")).write_text("{broken")
    await purge.scan_problems(store)
    problems = [
        p for p in await store.get_problems() if p.kind in {"damaged_source", "purge_receipt"}
    ]
    assert sorted(p.kind for p in problems) == ["damaged_source", "purge_receipt"]
    assert (await store.get_status()).counters.purge_problems == 2


async def test_status_never_walks_source_bundles_or_finished_receipts(store, monkeypatch):
    retention(store, 0)
    ingested = await orphan(store)
    (store.sources_root / ingested.source.id / "manifest.json").unlink()
    await purge.scan_problems(store)
    now = datetime.now(UTC)
    async with store.session_factory() as session:
        for number in range(40):
            session.add(
                Source(
                    id=f"01K4Q8Z2A0P1Q2R3S4T5U6V{number:03d}"[:26],
                    provider="plaud",
                    external_source_id=f"filler-{number}",
                    source_type="transcript",
                    content_identity="identity",
                    created_at=now,
                    updated_at=now,
                )
            )
        await session.commit()
    (purge.directory(store) / "done").mkdir(parents=True, exist_ok=True)
    (purge.directory(store) / "done" / ("d" * 64 + ".json")).write_text("{broken")

    walked = []

    def forbidden(name):
        def refuse(*args, **kwargs):
            walked.append(name)
            raise AssertionError(name)

        return refuse

    for name in ("_bundle", "damaged_bundles", "outcome"):
        monkeypatch.setattr(purge, name, forbidden(name))
    real_rglob = type(store.sources_root).rglob

    def counting_rglob(self, *args, **kwargs):
        if self.is_relative_to(store.sources_root):
            walked.append("rglob")
        return real_rglob(self, *args, **kwargs)

    monkeypatch.setattr(type(store.sources_root), "rglob", counting_rglob)
    counters = (await store.get_status()).counters
    problems = await store.get_problems()
    assert not walked
    assert counters.purge_problems == 1
    assert [p.kind for p in problems if p.kind.startswith(("purge", "damaged"))] == [
        "damaged_source"
    ]


async def test_preview_says_what_it_kept_and_separates_notes_from_sources(store):
    retention(store, 30)
    recent, _, _ = await deleted(store)
    ingested = await orphan(store)
    live = await store.create_note(CreateNote(title="Keeps the source"))
    live_path = store.notes_root / live.path
    live_path.write_text(fm.patch(live_path.read_text(), {"sources": [ingested.source.id]}))
    commit(store.notes_root)
    await reconcile_once(store)
    await age(store, ingested.note.id, datetime.now(UTC) - timedelta(days=60))
    preview = await store.preview_purge()
    assert [c.id for c in preview.candidates] == [ingested.note.id]
    assert preview.counts.sources == 0
    assert preview.counts.by_provider == {"plaud": PurgeBreakdown(notes=1)}
    assert preview.kept.notes_within_retention == 1
    assert preview.kept.sources_in_use == 1
    assert recent.id not in {c.id for c in preview.candidates}


async def abandoned_with_rows_intact(store):
    ingested = await orphan(store)
    preview = await store.preview_purge()
    unfinished = purge.read(store, preview.token)
    purge.save(store, {**unfinished, "state": "applying"})
    abandoned = await store.abandon_purge(AbandonPurge(token=preview.token))
    assert sorted(abandoned.remaining) == sorted([ingested.note.id, ingested.source.id])
    assert abandoned.completed == 0
    return ingested


def abandoned_problems(problems):
    return [p for p in problems if p.kind == "purge"]


async def test_restored_note_clears_its_abandoned_problem_and_is_never_listed(store):
    retention(store, 0)
    ingested = await abandoned_with_rows_intact(store)
    assert (await store.get_status()).counters.purge_problems == 1
    receipt = note_history.receipts(store)[ingested.note.id]
    (store.notes_root / "Returned.md").write_bytes(receipt.data())
    commit(store.notes_root)
    await reconcile_once(store)

    assert not abandoned_problems(await store.get_problems())
    assert (await store.get_status()).counters.purge_problems == 0
    assert not (await store.preview_purge()).candidates
    assert (store.sources_root / ingested.source.id).exists()
    async with store.session_factory() as session:
        assert (await session.get(Note, ingested.note.id)).state != "deleted"


async def test_new_live_citation_shrinks_the_abandoned_problem_and_protects_the_source(store):
    retention(store, 0)
    ingested = await abandoned_with_rows_intact(store)
    live = await store.create_note(CreateNote(title="Now cites the source"))
    live_path = store.notes_root / live.path
    live_path.write_text(fm.patch(live_path.read_text(), {"sources": [ingested.source.id]}))
    commit(store.notes_root)
    await reconcile_once(store)

    (problem,) = abandoned_problems(await store.get_problems())
    assert "0 listed items were removed. 1 were not" in problem.reason
    assert (await store.get_status()).counters.purge_problems == 1
    preview = await store.preview_purge()
    assert [c.id for c in preview.candidates] == [ingested.note.id]
    assert (store.sources_root / ingested.source.id).exists()


def damaged_problems(problems):
    return [p for p in problems if p.kind == "damaged_source"]


async def test_recovery_records_an_unverifiable_bundle_with_an_accurate_reason(store):
    retention(store, 0)
    ingested = await store.ingest(sample())
    bundle = store.sources_root / ingested.source.id
    artifact = next(bundle.glob("r*/*.txt"))
    content = artifact.read_bytes()
    artifact.unlink()
    with pytest.raises(SourcesFilesystemUnavailable):
        await store.rebuild_metadata()
    (problem,) = damaged_problems(await store.get_problems())
    assert problem.source_id == ingested.source.id
    assert "could not be verified during rebuild" in problem.reason
    assert "no readable manifest" not in problem.reason
    assert (await store.get_status()).counters.purge_problems == 1

    artifact.write_bytes(content)
    await store.rebuild_metadata()
    assert not damaged_problems(await store.get_problems())
    assert (await store.get_status()).counters.purge_problems == 0


async def test_purge_planning_does_not_clear_what_recovery_found(store):
    retention(store, 0)
    ingested = await orphan(store)
    bundle = store.sources_root / ingested.source.id
    artifact = next(bundle.glob("r*/*.txt"))
    content = artifact.read_bytes()
    artifact.unlink()
    with pytest.raises(SourcesFilesystemUnavailable):
        await store.rebuild_metadata()

    preview = await store.preview_purge()
    assert ingested.source.id in {c.id for c in preview.candidates}
    (problem,) = damaged_problems(await store.get_problems())
    assert "could not be verified" in problem.reason
    assert (await store.get_status()).counters.purge_problems == 1

    artifact.write_bytes(content)
    await store.preview_purge()
    assert damaged_problems(await store.get_problems())
    await store.rebuild_metadata()
    assert not damaged_problems(await store.get_problems())


async def test_an_authorized_purge_removes_what_recovery_recorded(store):
    retention(store, 0)
    ingested = await orphan(store)
    bundle = store.sources_root / ingested.source.id
    next(bundle.glob("r*/*.txt")).unlink()
    with pytest.raises(SourcesFilesystemUnavailable):
        await store.rebuild_metadata()
    assert len(damaged_problems(await store.get_problems())) == 1

    preview = await store.preview_purge()
    assert (await store.confirm_purge(confirmation(preview))).state == "complete"
    assert not bundle.exists()
    assert not damaged_problems(await store.get_problems())
    assert (await store.get_status()).counters.purge_problems == 0


async def test_a_manifest_unreadable_bundle_is_still_reported_as_such(store):
    retention(store, 0)
    ingested = await orphan(store)
    (store.sources_root / ingested.source.id / "manifest.json").unlink()
    await store.preview_purge()
    (problem,) = damaged_problems(await store.get_problems())
    assert "no readable manifest" in problem.reason
