import pytest
import sqlalchemy as sa

from coppermind.db.models import Note, RecordedRejection, Source


@pytest.mark.asyncio
async def test_counters(store, db_session):
    # Insert mock data
    db_session.add(
        RecordedRejection(kind="ingest", reference="source:1", reason="Payload too large")
    )
    db_session.add(
        RecordedRejection(kind="collision", reference="note:1", reason="duplicate identity")
    )

    note = Note(
        id="01H00000000000000000000000",
        path="Review/test.md",
        content_hash="hash",
        size_bytes=10,
        reviewed=False,
        state="unparsed",
        first_seen_at=sa.func.now(),
        updated_at=sa.func.now(),
    )
    db_session.add(note)

    source = Source(
        id="01H00000000000000000000001",
        provider="test",
        external_source_id="test-1",
        source_type="test",
        origin="test",
        current_revision=1,
        created_at=sa.func.now(),
        updated_at=sa.func.now(),
    )
    db_session.add(source)
    await db_session.commit()

    status = await store.get_status()
    assert status.counters.notes_awaiting_review == 1
    assert status.counters.rejected_ingests == 1
    assert status.counters.name_collisions == 1
    assert status.counters.unparseable_files == 1
    assert status.counters.sources == 1
