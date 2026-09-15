"""Source ingest ordering across the notes filesystem and PostgreSQL."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import coppermind_store.sources as sources_module
import pytest
import sqlalchemy as sa
from coppermind_store.notes import LocalStore

from coppermind import frontmatter as fm
from coppermind.db.models import Note, NoteSource, Source, SourceArtifact, SourceRevision
from coppermind.store_protocol import (
    IngestRequest,
    MetadataUnavailable,
    PayloadTooLarge,
)


def sample(external_id: str = "rec_8f3a2c19") -> IngestRequest:
    return IngestRequest.model_validate(
        {
            "source": {
                "provider": "plaud",
                "external_source_id": external_id,
                "source_type": "transcript",
                "origin": "Plaud NotePin",
                "captured_at": "2026-09-08T14:02:11-05:00",
                "metadata": {"duration_s": 2711, "language": "en"},
                "artifacts": [
                    {
                        "name": "transcript.txt",
                        "mime_type": "text/plain",
                        "content": "Scott: Let's start with the architecture review...",
                    },
                    {
                        "name": "plaud-summary.md",
                        "mime_type": "text/markdown",
                        "content": "## Summary\n- Reviewed the target architecture...",
                    },
                ],
            },
            "note": {
                "title": "Ameren Architecture Sync",
                "body": "## Key points\n- Target architecture agreed\n",
                "frontmatter": {
                    "date": "2026-09-08",
                    "type": "meeting",
                    "context": "customer",
                    "account": "Ameren",
                    "tags": ["architecture", "vcf"],
                },
            },
        }
    )


async def test_ingest_writes_an_immutable_bundle_linked_to_one_review_note(
    store: LocalStore, session_factory
):
    result = await store.ingest(sample())
    assert result.source.revision == 1
    assert result.source.created is True
    assert result.note.created is True

    source_root = store.sources_root / result.source.id
    manifest = json.loads((source_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_id"] == result.source.id
    assert manifest["external_source_id"] == "rec_8f3a2c19"
    assert manifest["current_revision"] == 1
    assert [item["name"] for item in manifest["revisions"][0]["artifacts"]] == [
        "transcript.txt",
        "plaud-summary.md",
    ]
    assert (
        (source_root / "r0001" / "transcript.txt").read_text(encoding="utf-8").startswith("Scott:")
    )
    claims = list(store.sources_root.glob(".external-id-*.json"))
    assert len(claims) == 1
    claim = json.loads(claims[0].read_text(encoding="utf-8"))
    assert claim == {
        "schema_version": 1,
        "provider": "plaud",
        "external_source_id": "rec_8f3a2c19",
        "source_id": result.source.id,
    }

    note_path = store.notes_root / result.note.path
    frontmatter, body = fm.parse(note_path.read_text(encoding="utf-8"))
    assert result.note.path == "Review/2026-09-08 Ameren Architecture Sync.md"
    assert frontmatter["sources"] == [result.source.id]
    assert body.startswith("# Ameren Architecture Sync")

    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceRevision)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceArtifact)) == 2
        link = (await session.execute(sa.select(NoteSource))).scalar_one()
        assert link.note_id == result.note.id
        assert link.source_id == result.source.id
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 1


async def test_t_ing_1_identical_replay_returns_the_original_without_duplicating(
    store: LocalStore, session_factory
):
    first = await store.ingest(sample())
    before = {
        path.relative_to(store.notes_root.parent): path.read_bytes()
        for path in store.notes_root.parent.rglob("*")
        if path.is_file()
    }

    replay = await store.ingest(sample())

    after = {
        path.relative_to(store.notes_root.parent): path.read_bytes()
        for path in store.notes_root.parent.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert replay.source.id == first.source.id
    assert replay.source.revision == 1
    assert replay.source.created is False
    assert replay.note.id == first.note.id
    assert replay.note.path == first.note.path
    assert replay.note.created is False
    assert [path.name for path in store.sources_root.iterdir() if path.is_dir()] == [
        first.source.id
    ]
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 1


async def test_oversize_ingest_is_refused_before_files_or_rows_are_written(
    store: LocalStore, session_factory
):
    body = dict(store.control.store.read("settings").body)
    body["limits"]["ingest_max_bytes"] = 512
    store.control.store.write("settings", body, if_revision=1)
    request = sample()
    request.source.artifacts[0].content = "x" * 1024

    with pytest.raises(PayloadTooLarge) as raised:
        await store.ingest(request)

    assert raised.value.limit_bytes == 512
    assert list(store.notes_root.rglob("*.md")) == []
    assert not store.sources_root.exists()
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 0


async def test_concurrent_duplicate_is_serialised_by_the_filesystem_claim(
    store: LocalStore, session_factory
):
    """The claim and its lock produce one create and one successful replay."""
    outcomes = await asyncio.gather(store.ingest(sample()), store.ingest(sample()))
    assert sorted(item.source.created for item in outcomes) == [False, True]
    assert outcomes[0].source.id == outcomes[1].source.id
    winner = next(item for item in outcomes if item.source.created)
    assert [path.name for path in store.sources_root.iterdir() if path.is_dir()] == [
        winner.source.id
    ]
    assert len(list(store.notes_root.rglob("*.md"))) == 1
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 1


class InterruptedIngest(BaseException):
    pass


async def test_interruption_before_the_note_leaves_no_bundle_or_rows(
    store: LocalStore, session_factory, monkeypatch: pytest.MonkeyPatch
):
    real_create = sources_module.create_exclusive_bytes

    def interrupt_note_write(path, data, **kwargs):
        # The claim, both artifacts and the manifest have landed. Interrupt
        # the final note write and verify no claim or bundle survives it.
        if path.is_relative_to(store.notes_root):
            raise InterruptedIngest()
        return real_create(path, data, **kwargs)

    monkeypatch.setattr(sources_module, "create_exclusive_bytes", interrupt_note_write)

    with pytest.raises(InterruptedIngest):
        await store.ingest(sample())

    assert list(store.notes_root.rglob("*.md")) == []
    assert list(store.sources_root.iterdir()) == []
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 0

    monkeypatch.setattr(sources_module, "create_exclusive_bytes", real_create)
    retried = await store.ingest(sample())
    assert (store.sources_root / retried.source.id / "manifest.json").is_file()


async def test_commit_failure_retry_resolves_the_durable_claim_and_repairs_the_mirror(
    store: LocalStore, session_factory, monkeypatch: pytest.MonkeyPatch
):
    real_transaction = sources_module.transaction

    @asynccontextmanager
    async def fail_at_commit(factory):
        async with real_transaction(factory) as session:
            yield session
            raise sa.exc.OperationalError("COMMIT", {}, OSError("connection lost"))

    monkeypatch.setattr(sources_module, "transaction", fail_at_commit)
    with pytest.raises(MetadataUnavailable):
        await store.ingest(sample())

    assert len(list(store.sources_root.glob(".external-id-*.json"))) == 1
    assert len([path for path in store.sources_root.iterdir() if path.is_dir()]) == 1
    assert len(list(store.notes_root.rglob("*.md"))) == 1
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 0

    monkeypatch.setattr(sources_module, "transaction", real_transaction)
    replay = await store.ingest(sample())

    assert replay.source.created is False
    assert replay.source.revision == 1
    assert replay.note.created is False
    assert len([path for path in store.sources_root.iterdir() if path.is_dir()]) == 1
    assert len(list(store.notes_root.rglob("*.md"))) == 1
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceRevision)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 1


async def test_t_ing_2_changed_content_appends_a_revision_and_keeps_the_review_note(
    store: LocalStore, session_factory
):
    first_request = sample()
    first = await store.ingest(first_request)
    note_path = store.notes_root / first.note.path
    edited = fm.patch(note_path.read_text(encoding="utf-8"), {"reviewed": True})
    edited = edited.replace("Target architecture agreed", "Captain's correction")
    note_path.write_text(edited, encoding="utf-8")
    note_bytes = note_path.read_bytes()

    changed = sample()
    changed.source.artifacts[0].content = "Scott: corrected source content"
    second = await store.ingest(changed)

    assert second.source.id == first.source.id
    assert second.source.revision == 2
    assert second.source.created is True
    assert second.note.id == first.note.id
    assert second.note.path == first.note.path
    assert second.note.created is False
    assert note_path.read_bytes() == note_bytes
    source_root = store.sources_root / first.source.id
    assert (source_root / "r0001" / "transcript.txt").read_text() == (
        "Scott: Let's start with the architecture review..."
    )
    assert (source_root / "r0002" / "transcript.txt").read_text() == (
        "Scott: corrected source content"
    )
    manifest = json.loads((source_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["current_revision"] == 2
    assert [item["revision"] for item in manifest["revisions"]] == [1, 2]
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceRevision)) == 2
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 1


async def test_retry_after_a_revision_commit_failure_returns_the_completed_revision(
    store: LocalStore, session_factory, monkeypatch: pytest.MonkeyPatch
):
    first = await store.ingest(sample())
    changed = sample()
    changed.source.artifacts[0].content = "Scott: corrected source content"
    real_transaction = sources_module.transaction

    @asynccontextmanager
    async def fail_at_commit(factory):
        async with real_transaction(factory) as session:
            yield session
            raise sa.exc.OperationalError("COMMIT", {}, OSError("connection lost"))

    monkeypatch.setattr(sources_module, "transaction", fail_at_commit)
    with pytest.raises(MetadataUnavailable):
        await store.ingest(changed)

    source_root = store.sources_root / first.source.id
    assert (source_root / "r0002" / "transcript.txt").is_file()
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceRevision)) == 1

    monkeypatch.setattr(sources_module, "transaction", real_transaction)
    replay = await store.ingest(changed)
    assert replay.source.id == first.source.id
    assert replay.source.revision == 2
    assert replay.source.created is False
    assert replay.note.id == first.note.id
    assert replay.note.created is False
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceRevision)) == 2


async def test_replay_cannot_overwrite_a_concurrent_note_edit(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
):
    first = await store.ingest(sample())
    note_path = store.notes_root / first.note.path
    reached_mirror = asyncio.Event()
    allow_replay = asyncio.Event()
    real_ensure_mirror = sources_module._ensure_mirror

    async def held_ensure_mirror(*args, **kwargs):
        reached_mirror.set()
        await allow_replay.wait()
        return await real_ensure_mirror(*args, **kwargs)

    monkeypatch.setattr(sources_module, "_ensure_mirror", held_ensure_mirror)
    replay_task = asyncio.create_task(store.ingest(sample()))
    await reached_mirror.wait()
    edited = fm.patch(note_path.read_text(encoding="utf-8"), {"reviewed": True})
    edited = edited.replace("Target architecture agreed", "Edited during replay")
    note_path.write_text(edited, encoding="utf-8")
    allow_replay.set()
    replay = await replay_task

    assert replay.source.created is False
    assert replay.note.created is False
    assert note_path.read_text(encoding="utf-8") == edited
