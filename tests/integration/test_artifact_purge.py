"""Retention purge of removed artifact archives: same dry run, confirm and abandon flow."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from coppermind_store import purge
from coppermind_store.artifacts import manifest_path

from coppermind.db.models import Artifact
from coppermind.purge import AbandonPurge, ConfirmPurge, PurgeBreakdown
from coppermind.store_protocol import (
    CreateArtifact,
    CreateNote,
    NotesFilesystemUnavailable,
    PurgeConflict,
)
from tests.integration.test_artifacts import upload
from tests.integration.test_purge import confirmation, retention


def age_removal(store, artifact_id, days):
    path = manifest_path(store, artifact_id)
    manifest = json.loads(path.read_text())
    manifest["removed_at"] = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    path.write_text(json.dumps(manifest))


async def removed(store, name, data, days):
    created = await store.create_artifact(upload(name, data))
    await store.remove_artifact(created.id)
    age_removal(store, created.id, days)
    return created


async def test_a_removed_artifact_past_the_window_is_previewed_and_purged(store):
    retention(store, 30)
    old = await store.create_artifact(upload("old.txt", b"old bytes\n"))
    note = await store.create_note(
        CreateNote(
            title="Cites the old one",
            frontmatter={"type": "reference", "context": "internal", "artifacts": [old.id]},
        )
    )
    await store.remove_artifact(old.id)
    age_removal(store, old.id, 31)
    recent = await removed(store, "recent.txt", b"recent bytes\n", 5)
    live = await store.create_artifact(upload("live.txt", b"live bytes\n"))

    preview = await store.preview_purge()

    assert [(c.kind, c.id) for c in preview.candidates] == [("artifact", old.id)]
    assert preview.counts.artifacts == 1 and preview.counts.notes == 0
    assert preview.counts.by_provider == {"Artifacts": PurgeBreakdown(artifacts=1)}
    assert preview.kept.artifacts_within_retention == 1
    assert (store.artifacts_root / old.id / "r0001" / "old.txt").exists()

    done = await store.confirm_purge(confirmation(preview))

    assert done.state == "complete"
    assert not (store.artifacts_root / old.id).exists()
    assert (store.artifacts_root / recent.id / "r0001" / "recent.txt").exists()
    assert (store.artifacts_root / live.id / "r0001" / "live.txt").exists()
    assert (store.notes_root / live.path).exists()
    assert (await store.get_note(note.id)).frontmatter["artifacts"] == [old.id]
    assert not (await store.preview_purge()).candidates
    assert (await store.rebuild_metadata()).counts["recovered_artifacts"] == 1


async def test_an_artifact_that_was_never_removed_is_never_a_candidate(store):
    retention(store, 0)
    live = await store.create_artifact(upload("live.txt", b"live bytes\n"))
    link = await store.create_artifact(
        CreateArtifact(kind="artifact", url="https://example.test/kept", title="Kept link")
    )

    preview = await store.preview_purge()

    assert preview.candidates == []
    assert (store.artifacts_root / live.id / "manifest.json").exists()
    assert (store.artifacts_root / link.id / "manifest.json").exists()


async def test_the_window_boundary_is_inclusive(store):
    retention(store, 0)
    gone = await removed(store, "gone.txt", b"gone\n", 0)
    preview = await store.preview_purge()
    assert [c.id for c in preview.candidates] == [gone.id]


async def test_a_changed_archive_refuses_the_whole_purge(store):
    retention(store, 0)
    gone = await removed(store, "gone.txt", b"gone\n", 1)
    preview = await store.preview_purge()
    (store.artifacts_root / gone.id / "r0001" / "gone.txt").write_bytes(b"changed\n")

    with pytest.raises(PurgeConflict):
        await store.confirm_purge(confirmation(preview))

    assert (store.artifacts_root / gone.id / "r0001" / "gone.txt").read_bytes() == b"changed\n"


async def test_an_interrupted_artifact_purge_can_be_retried_or_abandoned(store, monkeypatch):
    retention(store, 0)
    gone = await removed(store, "gone.txt", b"gone\n", 1)
    preview = await store.preview_purge()
    real = purge._unlink

    def fail(store, item):
        raise OSError("simulated disk error")

    monkeypatch.setattr(purge, "_unlink", fail)
    with pytest.raises(NotesFilesystemUnavailable):
        await store.confirm_purge(confirmation(preview))
    assert purge.pending_ids(store, "artifact") == {gone.id}

    abandoned = await store.abandon_purge(AbandonPurge(token=preview.token))
    assert abandoned.state == "abandoned" and abandoned.remaining == [gone.id]
    assert (store.artifacts_root / gone.id / "r0001" / "gone.txt").exists()

    monkeypatch.setattr(purge, "_unlink", real)
    fresh = await store.preview_purge()
    assert [c.id for c in fresh.candidates] == [gone.id]
    assert (
        await store.confirm_purge(
            ConfirmPurge(token=fresh.token, confirmation="Purge listed items")
        )
    ).state == "complete"
    assert not (store.artifacts_root / gone.id).exists()
    async with store.session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Artifact)) == 0
