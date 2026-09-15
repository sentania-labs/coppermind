"""Rollback behavior for the reconciled note-path mirror."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coppermind.db.models import Note

REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic(database_url: str, password_file: Path, command: str, revision: str) -> None:
    environment = {
        **os.environ,
        "COPPERMIND_DATABASE_URL": database_url,
        "COPPERMIND_DB_PASSWORD_FILE": str(password_file),
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", command, revision],
        cwd=REPO_ROOT,
        env=environment,
        check=True,
        capture_output=True,
    )


async def test_downgrade_discards_rebuildable_duplicate_paths_before_restoring_uniqueness(
    migrated_database: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    now = datetime.now(tz=UTC)
    shared = "Review/Reused.md"
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                Note(
                    id="missing-note",
                    path=shared,
                    title="Old note",
                    content_hash="sha256:old",
                    size_bytes=1,
                    frontmatter={},
                    state="missing",
                    first_seen_at=now - timedelta(days=1),
                    updated_at=now,
                ),
                Note(
                    id="live-note",
                    path=shared,
                    title="Replacement",
                    content_hash="sha256:live",
                    size_bytes=1,
                    frontmatter={},
                    state="ok",
                    first_seen_at=now,
                    updated_at=now - timedelta(hours=1),
                ),
            ]
        )

    password = tmp_path / "postgres-password"
    password.write_text("coppermind\n", encoding="utf-8")
    try:
        _alembic(migrated_database, password, "downgrade", "0002")
        async with session_factory() as session:
            ids = (await session.scalars(sa.select(Note.id).where(Note.path == shared))).all()
            constraint = await session.scalar(
                sa.text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'notes'::regclass AND conname = 'uq_notes_path'"
                )
            )
        assert ids == ["live-note"]
        assert constraint == "uq_notes_path"
    finally:
        _alembic(migrated_database, password, "upgrade", "head")
