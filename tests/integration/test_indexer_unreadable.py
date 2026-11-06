"""Catch-up remembers failures instead of repeatedly reading the same batch."""

from datetime import UTC, datetime

import sqlalchemy as sa
from coppermind_store import filing, indexer

from coppermind.db.models import NoteSource, Source
from coppermind.store_protocol import CreateNote


async def _approve(store, source_ids, title="Approver"):
    """A reviewed, filed note that approves revision 1 of each source."""
    created = await store.create_note(
        CreateNote(
            title=title,
            folder="2026-09",
            body="Body",
            frontmatter={
                "date": "2026-09-08",
                "type": "reference",
                "context": "internal",
                "reviewed": True,
            },
        )
    )
    await filing.file_reviewed(store)
    now = datetime.now(UTC)
    async with store.session_factory() as session, session.begin():
        session.add_all(
            NoteSource(note_id=created.id, source_id=ref, approved_revision=1, created_at=now)
            for ref in source_ids
        )
    return created


async def test_missing_projection_batch_is_not_retried(store, monkeypatch):
    now = datetime.now(UTC)
    async with store.session_factory() as session, session.begin():
        session.add_all(
            [
                Source(
                    id=f"missing-{i}",
                    provider="test",
                    external_source_id=str(i),
                    source_type="transcript",
                    current_revision=1,
                    content_identity=str(i),
                    created_at=now,
                    updated_at=now,
                )
                for i in range(200)
            ]
        )
    created = await _approve(store, [f"missing-{i}" for i in range(200)], "Fresh work")
    first = await indexer.catch_up(store)

    def unexpected_read(*args):
        raise AssertionError("unchanged failed manifest was read again")

    with monkeypatch.context() as patch:
        patch.setattr(indexer, "_projection_path", unexpected_read)
        second = await indexer.catch_up(store)
    print("first catch-up:", first, "next minute:", second)
    assert first["unreadable"] == 200
    assert second["unreadable"] == 0
    assert second["skipped_known_unreadable"] == 200

    # Known failures leave the whole next batch available for real work.
    from coppermind.db.models import SearchDocument

    async with store.session_factory() as session, session.begin():
        await session.execute(sa.delete(SearchDocument).where(SearchDocument.ref_id == created.id))
    following = await indexer.catch_up(store)
    assert following["indexed"] == 1
    assert following["skipped_known_unreadable"] == 200

    # A source its note has not approved is not drawn at all, so it is neither
    # read nor marked.
    async with store.session_factory() as session, session.begin():
        session.add(
            Source(
                id="pending",
                provider="test",
                external_source_id="pending",
                source_type="transcript",
                current_revision=1,
                content_identity="pending",
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            NoteSource(
                note_id=created.id, source_id="pending", approved_revision=None, created_at=now
            )
        )
    pending = await indexer.catch_up(store)
    assert pending["unreadable"] == 0
    assert pending["skipped_known_unreadable"] == 200
    assert (await store.get_search_index_status()).known_unreadable == 200
    async with store.session_factory() as session:
        from coppermind.db.models import SearchUnreadable

        assert await session.get(SearchUnreadable, ("source", "pending")) is None


async def test_source_revision_manifest_and_projection_changes_retry(store):
    import json

    from coppermind.db.models import SearchUnreadable

    now = datetime.now(UTC)
    async with store.session_factory() as session, session.begin():
        session.add(
            Source(
                id="retry",
                provider="test",
                external_source_id="retry",
                source_type="transcript",
                current_revision=1,
                content_identity="x",
                created_at=now,
                updated_at=now,
            )
        )
    await _approve(store, ["retry"])
    assert (await indexer.catch_up(store))["unreadable"] == 1
    assert (await indexer.catch_up(store))["skipped_known_unreadable"] == 1
    async with store.session_factory() as session, session.begin():
        await session.execute(
            sa.update(Source).where(Source.id == "retry").values(current_revision=2)
        )
        await session.execute(
            sa.update(NoteSource).where(NoteSource.source_id == "retry").values(approved_revision=2)
        )
    assert (await indexer.catch_up(store))["unreadable"] == 1
    manifest = store.sources_root / "retry" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"projection_path": "Sources/retry.md"}))
    assert (await indexer.catch_up(store))["unreadable"] == 1
    async with store.session_factory() as session:
        mark = await session.get(SearchUnreadable, ("source", "retry"))
        assert mark.version == "r2"
        assert mark.path == "Sources/retry.md"
        assert "FileNotFoundError" in mark.reason
    page = store.notes_root / "Sources/retry.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\nreviewed: true\nmanaged: true\nsource_id: retry\n"
        "source_revision: 2\n---\nRestored projection"
    )
    restored = await indexer.catch_up(store)
    assert restored["unreadable"] == 0
    assert restored["indexed"] == 1
    async with store.session_factory() as session:
        assert await session.get(SearchUnreadable, ("source", "retry")) is None


async def test_failure_log_once_status_problems_and_rebuild_retry(store):
    from structlog.testing import capture_logs

    from coppermind.db.models import SearchUnreadable

    now = datetime.now(UTC)
    async with store.session_factory() as session, session.begin():
        session.add(
            Source(
                id="log-once",
                provider="test",
                external_source_id="log",
                source_type="transcript",
                current_revision=1,
                content_identity="x",
                created_at=now,
                updated_at=now,
            )
        )
    await _approve(store, ["log-once"])
    with capture_logs() as logs:
        await indexer.catch_up(store)
        await indexer.catch_up(store)
    failures = [item for item in logs if item["event"] == "search entry unreadable"]
    assert len(failures) == 1
    assert failures[0]["path"].endswith("log-once/manifest.json")
    assert failures[0]["reason"].startswith("manifest FileNotFoundError:")
    assert (await store.get_search_index_status()).known_unreadable == 1
    assert (await store.get_status()).counters.known_unreadable == 1
    problems = [p for p in await store.get_problems() if p.kind == "search_unreadable"]
    assert len(problems) == 1
    assert problems[0].reason == failures[0]["reason"]
    async with store.session_factory() as session:
        assert await session.get(SearchUnreadable, ("source", "log-once")) is not None
    with capture_logs() as retried:
        await indexer.rebuild(store)
    assert len([item for item in retried if item["event"] == "search entry unreadable"]) == 1
    assert (await indexer.catch_up(store))["skipped_known_unreadable"] == 1


async def test_note_path_hash_and_file_repairs_retry(store):
    from coppermind.db.models import Note, SearchDocument, SearchUnreadable

    created = await store.create_note(
        CreateNote(
            title="Retry note",
            folder="2026-09",
            body="Expected body",
            frontmatter={
                "date": "2026-09-08",
                "type": "reference",
                "context": "internal",
                "reviewed": True,
            },
        )
    )
    await filing.file_reviewed(store)
    async with store.session_factory() as session:
        row = await session.get(Note, created.id)
        path, version = row.path, row.content_hash
    file = store.notes_root / path
    original = file.read_bytes()
    file.unlink()
    async with store.session_factory() as session, session.begin():
        await session.execute(sa.delete(SearchDocument))
    assert (await indexer.catch_up(store))["unreadable"] == 1
    assert (await indexer.catch_up(store))["skipped_known_unreadable"] == 1
    async with store.session_factory() as session, session.begin():
        await session.execute(
            sa.update(Note).where(Note.id == created.id).values(path="2026-09/moved.md")
        )
    assert (await indexer.catch_up(store))["unreadable"] == 1
    async with store.session_factory() as session, session.begin():
        await session.execute(
            sa.update(Note).where(Note.id == created.id).values(content_hash="changed")
        )
    assert (await indexer.catch_up(store))["unreadable"] == 1
    async with store.session_factory() as session, session.begin():
        await session.execute(
            sa.update(Note).where(Note.id == created.id).values(content_hash=version)
        )
    moved = store.notes_root / "2026-09/moved.md"
    moved.parent.mkdir(parents=True, exist_ok=True)
    moved.write_bytes(original)
    assert (await indexer.catch_up(store))["indexed"] == 1
    async with store.session_factory() as session:
        assert await session.get(SearchUnreadable, ("note", created.id)) is None


async def test_pass_only_reads_one_batch_of_unmarked_sources(store, monkeypatch):
    now = datetime.now(UTC)
    async with store.session_factory() as session, session.begin():
        session.add_all(
            [
                Source(
                    id=f"bulk-{i}",
                    provider="test",
                    external_source_id=str(i),
                    source_type="transcript",
                    current_revision=1,
                    content_identity=str(i),
                    created_at=now,
                    updated_at=now,
                )
                for i in range(indexer.CATCH_UP_BATCH + 50)
            ]
        )
    await _approve(store, [f"bulk-{i}" for i in range(indexer.CATCH_UP_BATCH + 50)])
    reads: list[str] = []
    original = indexer._projection_path

    def counting(sources_root, source_id, reasons=None):
        reads.append(source_id)
        return original(sources_root, source_id, reasons)

    monkeypatch.setattr(indexer, "_projection_path", counting)
    counts = await indexer.catch_up(store)
    assert counts["unreadable"] == indexer.CATCH_UP_BATCH
    assert len(reads) == indexer.CATCH_UP_BATCH


async def test_mark_clears_when_entry_stops_being_a_candidate(store):
    from coppermind.db.models import Note, SearchDocument, SearchUnreadable

    created = await store.create_note(
        CreateNote(
            title="Goes unparsed",
            folder="2026-09",
            body="Body",
            frontmatter={
                "date": "2026-09-08",
                "type": "reference",
                "context": "internal",
                "reviewed": True,
            },
        )
    )
    await filing.file_reviewed(store)
    async with store.session_factory() as session:
        path = (await session.get(Note, created.id)).path
    (store.notes_root / path).unlink()
    async with store.session_factory() as session, session.begin():
        await session.execute(sa.delete(SearchDocument))
    assert (await indexer.catch_up(store))["unreadable"] == 1
    async with store.session_factory() as session:
        assert await session.get(SearchUnreadable, ("note", created.id)) is not None
    async with store.session_factory() as session, session.begin():
        await session.execute(sa.update(Note).where(Note.id == created.id).values(state="unparsed"))
    await indexer.catch_up(store)
    async with store.session_factory() as session:
        assert await session.get(SearchUnreadable, ("note", created.id)) is None
    assert (await store.get_search_index_status()).known_unreadable == 0


async def test_transient_read_error_is_retried_not_marked(store):
    import os

    import pytest
    from structlog.testing import capture_logs

    from coppermind.db.models import Note, SearchDocument

    if os.geteuid() == 0:
        pytest.skip("permission errors are not raised for root")
    created = await store.create_note(
        CreateNote(
            title="Blip",
            folder="2026-09",
            body="Body",
            frontmatter={
                "date": "2026-09-08",
                "type": "reference",
                "context": "internal",
                "reviewed": True,
            },
        )
    )
    await filing.file_reviewed(store)
    async with store.session_factory() as session:
        path = (await session.get(Note, created.id)).path
    file = store.notes_root / path
    async with store.session_factory() as session, session.begin():
        await session.execute(sa.delete(SearchDocument))
    file.chmod(0)
    try:
        with capture_logs() as logs:
            first = await indexer.catch_up(store)
            second = await indexer.catch_up(store)
    finally:
        file.chmod(0o644)
    assert first["unreadable"] == 1
    assert second["unreadable"] == 1
    assert second["skipped_known_unreadable"] == 0
    assert (await store.get_search_index_status()).known_unreadable == 0
    assert any(item["event"] == "search entry unreadable" for item in logs)
    assert (await indexer.catch_up(store))["indexed"] == 1


async def test_transient_manifest_read_error_is_retried_not_marked(store):
    import json
    import os

    import pytest

    from coppermind.db.models import SearchUnreadable

    if os.geteuid() == 0:
        pytest.skip("permission errors are not raised for root")
    now = datetime.now(UTC)
    async with store.session_factory() as session, session.begin():
        session.add(
            Source(
                id="blip",
                provider="test",
                external_source_id="blip",
                source_type="transcript",
                current_revision=1,
                content_identity="x",
                created_at=now,
                updated_at=now,
            )
        )
    await _approve(store, ["blip"])
    manifest = store.sources_root / "blip" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"projection_path": "Sources/blip.md"}))
    manifest.chmod(0)
    try:
        first = await indexer.catch_up(store)
        second = await indexer.catch_up(store)
    finally:
        manifest.chmod(0o644)
    assert first["unreadable"] == 1
    assert second["unreadable"] == 1
    assert second["skipped_known_unreadable"] == 0
    assert (await store.get_search_index_status()).known_unreadable == 0
    assert (await indexer.catch_up(store))["unreadable"] == 1
    async with store.session_factory() as session:
        mark = await session.get(SearchUnreadable, ("source", "blip"))
        assert mark is not None
        assert "FileNotFoundError" in mark.reason


async def test_unapproved_source_is_neither_drawn_nor_marked(store):
    import json

    from coppermind.db.models import SearchDocument, SearchUnreadable

    now = datetime.now(UTC)
    async with store.session_factory() as session, session.begin():
        session.add_all(
            Source(
                id=ref,
                provider="test",
                external_source_id=ref,
                source_type="transcript",
                current_revision=1,
                content_identity="x",
                created_at=now,
                updated_at=now,
            )
            for ref in ("orphan", "gone")
        )
    manifest = store.sources_root / "orphan" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"projection_path": "Sources/orphan.md"}))
    page = store.notes_root / "Sources/orphan.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\nreviewed: true\nmanaged: true\nsource_id: orphan\n"
        "source_revision: 1\n---\nStill on disk"
    )

    for _ in range(2):
        counts = await indexer.catch_up(store)
        assert counts["indexed"] == 0
        assert counts["unreadable"] == 0
        assert counts["skipped_known_unreadable"] == 0
    async with store.session_factory() as session:
        for ref in ("orphan", "gone"):
            assert await session.get(SearchUnreadable, ("source", ref)) is None
            assert await session.get(SearchDocument, ("source", ref)) is None
    assert (await store.get_search_index_status()).known_unreadable == 0

    created = await _approve(store, ["orphan", "gone"], "Opens it")
    async with store.session_factory() as session, session.begin():
        await session.execute(sa.delete(SearchDocument))
    third = await indexer.catch_up(store)
    assert third["indexed"] == 2
    assert third["unreadable"] == 1
    async with store.session_factory() as session:
        assert await session.get(SearchDocument, ("note", created.id)) is not None
        assert await session.get(SearchDocument, ("source", "orphan")) is not None
        assert await session.get(SearchUnreadable, ("source", "orphan")) is None
        assert await session.get(SearchUnreadable, ("source", "gone")) is not None
