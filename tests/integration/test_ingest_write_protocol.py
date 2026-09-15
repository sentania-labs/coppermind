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
    SourceAlreadyExists,
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


async def test_repeated_external_id_is_refused_without_mutating_or_duplicating(
    store: LocalStore, session_factory
):
    first = await store.ingest(sample())
    before = {
        path.relative_to(store.notes_root.parent): path.read_bytes()
        for path in store.notes_root.parent.rglob("*")
        if path.is_file()
    }

    with pytest.raises(SourceAlreadyExists):
        await store.ingest(sample())

    after = {
        path.relative_to(store.notes_root.parent): path.read_bytes()
        for path in store.notes_root.parent.rglob("*")
        if path.is_file()
    }
    assert after == before
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


async def test_concurrent_duplicate_is_refused_by_the_filesystem_claim(
    store: LocalStore, session_factory
):
    """The exclusive filesystem claim lets only one concurrent ingest proceed."""
    outcomes = await asyncio.gather(
        store.ingest(sample()), store.ingest(sample()), return_exceptions=True
    )
    refusals = [item for item in outcomes if isinstance(item, BaseException)]
    assert len(refusals) == 1, outcomes
    assert isinstance(refusals[0], SourceAlreadyExists)

    winner = next(item for item in outcomes if not isinstance(item, BaseException))
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


async def test_commit_failure_retains_claim_and_refuses_a_duplicate_retry(
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
    with pytest.raises(SourceAlreadyExists):
        await store.ingest(sample())

    assert len([path for path in store.sources_root.iterdir() if path.is_dir()]) == 1
    assert len(list(store.notes_root.rglob("*.md"))) == 1
