"""Migration 0016 removes what the earlier release kept about deleted notes, and only that."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coppermind.db.models import Note, NoteSource, Source
from coppermind.settings import Wiring

REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic(database_url: str, wiring: Wiring, command: str, revision: str) -> None:
    password = wiring.db_password_file
    assert password is not None
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", command, revision],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "COPPERMIND_DATABASE_URL": database_url,
            "COPPERMIND_DB_PASSWORD_FILE": str(password),
            "COPPERMIND_DATA_DIR": str(wiring.data_dir),
        },
        check=True,
        capture_output=True,
    )


def _note(note_id: str, state: str) -> Note:
    now = datetime.now(tz=UTC)
    return Note(
        id=note_id,
        path=f"Review/{note_id}.md",
        title=note_id,
        content_hash=f"sha256:{note_id}",
        size_bytes=1,
        frontmatter={},
        state=state,
        first_seen_at=now,
        updated_at=now,
    )


async def test_upgrade_drops_deleted_and_missing_records_and_keeps_live_ones(
    migrated_database: str,
    session_factory: async_sessionmaker[AsyncSession],
    wiring: Wiring,
):
    now = datetime.now(tz=UTC)
    state = wiring.state_dir
    async with session_factory() as session, session.begin():
        session.add_all([_note("live", "ok"), _note("gone", "deleted"), _note("lost", "missing")])
        session.add(
            Source(
                id="source-1",
                provider="test",
                external_source_id="one",
                source_type="note",
                content_identity="sha256:one",
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add_all(
            [
                NoteSource(note_id="live", source_id="source-1", created_at=now),
                NoteSource(note_id="gone", source_id="source-1", created_at=now),
            ]
        )
    leftovers = [
        state / "note-recovery" / "gone-abc.json",
        state / "note-recovery" / "restores" / "gone-token.json",
        state / "note-recovery" / "observed" / "gone.json",
        state / "admin-removed" / "0123456789abcdef0123456789abcdef.json",
        state / "cleanup_count.json",
        state / "purge-problems.json",
    ]
    kept = [state / "filing" / "live.json", state / "settings.yaml"]
    for path in [*leftovers, *kept]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    _alembic(migrated_database, wiring, "downgrade", "0015")
    _alembic(migrated_database, wiring, "upgrade", "0016")

    async with session_factory() as session:
        notes = set((await session.scalars(sa.select(Note.id))).all())
        links = set((await session.execute(sa.select(NoteSource.note_id))).scalars().all())
        sources = set((await session.scalars(sa.select(Source.id))).all())
    assert notes == {"live"}
    assert links == {"live"}
    assert sources == {"source-1"}
    assert not any(path.exists() for path in leftovers)
    assert all(path.exists() for path in kept)

    _alembic(migrated_database, wiring, "downgrade", "0015")
    _alembic(migrated_database, wiring, "upgrade", "0016")
    async with session_factory() as session:
        assert set((await session.scalars(sa.select(Note.id))).all()) == {"live"}
    assert all(path.exists() for path in kept)
