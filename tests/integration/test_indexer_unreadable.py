"""Catch-up remembers failures instead of repeatedly reading the same batch."""

from datetime import UTC, datetime

import sqlalchemy as sa
from coppermind_store import filing, indexer

from coppermind.db.models import Source


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
    from coppermind.store_protocol import CreateNote

    created = await store.create_note(
        CreateNote(
            title="Fresh work",
            folder="2026-09",
            body="Index me",
            frontmatter={
                "date": "2026-09-08",
                "type": "reference",
                "context": "internal",
                "reviewed": True,
            },
        )
    )
    await filing.file_reviewed(store)
    async with store.session_factory() as session, session.begin():
        await session.execute(sa.delete(SearchDocument).where(SearchDocument.ref_id == created.id))
    following = await indexer.catch_up(store)
    assert following["indexed"] == 1
    assert following["skipped_known_unreadable"] == 200

    # Approval of one source must not make another pending source eligible.
    from coppermind.db.models import NoteSource

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
        session.add_all(
            [
                NoteSource(
                    note_id=created.id, source_id="missing-0", approved_revision=1, created_at=now
                ),
                NoteSource(
                    note_id=created.id, source_id="pending", approved_revision=None, created_at=now
                ),
            ]
        )
    pending = await indexer.catch_up(store)
    assert pending["unreadable"] == 0
    assert (await store.get_search_index_status()).known_unreadable == 200


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
    assert (await indexer.catch_up(store))["unreadable"] == 1
    assert (await indexer.catch_up(store))["skipped_known_unreadable"] == 1
    async with store.session_factory() as session, session.begin():
        await session.execute(
            sa.update(Source).where(Source.id == "retry").values(current_revision=2)
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
    assert (await indexer.catch_up(store))["unreadable"] == 0
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
    from coppermind.store_protocol import CreateNote

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
    (store.notes_root / "2026-09/moved.md").write_bytes(original)
    assert (await indexer.catch_up(store))["indexed"] == 1
    async with store.session_factory() as session:
        assert await session.get(SearchUnreadable, ("note", created.id)) is None
