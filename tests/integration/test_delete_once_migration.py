"""Migration 0016 removes what the earlier release kept about deleted items, and only that."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from coppermind_store.sources import _external_id_claim_path
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coppermind.db.models import Note, NoteSource, SearchDocument, Source
from coppermind.ids import new_id
from coppermind.settings import Wiring
from coppermind.statefiles import StateStore

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


def _note(note_id: str, state: str, *sources: str) -> Note:
    now = datetime.now(tz=UTC)
    return Note(
        id=note_id,
        path=f"Review/{note_id}.md",
        title=note_id,
        content_hash=f"sha256:{note_id}",
        size_bytes=1,
        frontmatter={},
        source_ids=list(sources),
        state=state,
        first_seen_at=now,
        updated_at=now,
    )


def _source(source_id: str, name: str) -> Source:
    now = datetime.now(tz=UTC)
    return Source(
        id=source_id,
        provider="test",
        external_source_id=name,
        source_type="note",
        content_identity=f"sha256:{name}",
        created_at=now,
        updated_at=now,
    )


def _bundle(wiring: Wiring, source_id: str, name: str, opening: str) -> list[Path]:
    """A source bundle on disk, with its claim, and what to look for afterwards."""
    bundle = wiring.sources_dir / source_id
    (bundle / "r0001").mkdir(parents=True)
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "source_id": source_id,
                "note_id": opening,
                "provider": "test",
                "external_source_id": name,
            }
        )
    )
    (bundle / "r0001" / "transcript.txt").write_text("words")
    claim = _external_id_claim_path(wiring.sources_dir, "test", name)
    claim.write_text("{}")
    return [bundle, claim]


def _archive(wiring: Wiring, artifact_id: str, **marks: str) -> Path:
    archive = wiring.artifacts_dir / artifact_id
    (archive / "r0001").mkdir(parents=True)
    (archive / "r0001" / "file.txt").write_text("bytes")
    (archive / "manifest.json").write_text(json.dumps({"artifact_id": artifact_id, **marks}))
    return archive


async def test_upgrade_removes_deleted_records_and_keeps_everything_else(
    migrated_database: str,
    session_factory: async_sessionmaker[AsyncSession],
    wiring: Wiring,
):
    now = datetime.now(tz=UTC)
    state = wiring.state_dir
    live, gone, lost, filed = (new_id() for _ in range(4))
    only_gone, shared, only_live = (new_id() for _ in range(3))
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                _note(live, "ok", shared, only_live),
                _note(gone, "deleted", only_gone, shared),
                _note(lost, "missing"),
                _note(filed, "missing"),
            ]
        )
        session.add_all(
            [_source(only_gone, "one"), _source(shared, "two"), _source(only_live, "three")]
        )
        await session.flush()
        session.add_all(
            [
                NoteSource(note_id=gone, source_id=only_gone, created_at=now),
                NoteSource(note_id=gone, source_id=shared, created_at=now),
                NoteSource(note_id=live, source_id=shared, created_at=now),
                NoteSource(note_id=live, source_id=only_live, created_at=now),
            ]
        )
        session.add(
            SearchDocument(
                kind="source",
                ref_id=only_gone,
                path="x.md",
                title="t",
                body="b",
                version="r1",
                indexed_at=now,
            )
        )
    page = wiring.notes_dir / "_Sources" / "One.md"
    page.parent.mkdir(parents=True)
    page.write_text(f"---\nmanaged: true\nsource_id: {only_gone}\n---\nGenerated.\n")
    removed_files = [
        page,
        *_bundle(wiring, only_gone, "one", gone),
        state / "note-recovery" / f"{gone}-abc.json",
        state / "note-recovery" / "restores" / f"{gone}-token.json",
        state / "note-recovery" / "observed" / f"{gone}.json",
        state / "admin-removed" / "0123456789abcdef0123456789abcdef.json",
        state / "cleanup_count.json",
        state / "purge-problems.json",
        state / "purges" / "done" / "token.json",
        state / "filing" / f"{gone}.json",
    ]
    kept_files = [
        *_bundle(wiring, shared, "two", gone),
        *_bundle(wiring, only_live, "three", live),
        state / "filing" / f"{live}.json",
        # A missing note's saved filing copy is what restores it, so it stays.
        state / "filing" / f"{filed}.json",
    ]
    for path in [*removed_files, *kept_files]:
        if path.suffix == ".json" and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
    StateStore(state).write(
        "settings", {"general": {"timezone": "UTC"}, "retention": {"days": 7}}, if_revision=None
    )
    removed_archives = [
        _archive(wiring, new_id(), removed_at="2026-01-01T00:00:00+00:00", removed_reason=reason)
        for reason in ("removed in Admin", "removed through the API", "deleted in Obsidian")
    ]
    kept_archives = [
        _archive(wiring, new_id()),
        # Audio removed after review keeps its manifest as the receipt.
        _archive(
            wiring,
            new_id(),
            removed_at="2026-01-01T00:00:00+00:00",
            removed_reason="recording removed after review",
        ),
    ]

    _alembic(migrated_database, wiring, "downgrade", "0015")
    _alembic(migrated_database, wiring, "upgrade", "0016")

    async with session_factory() as session:
        notes = set((await session.scalars(sa.select(Note.id))).all())
        links = set((await session.execute(sa.select(NoteSource.note_id))).scalars().all())
        sources = set((await session.scalars(sa.select(Source.id))).all())
        documents = await session.scalar(sa.select(sa.func.count()).select_from(SearchDocument))
    # The deleted note is gone; the missing ones stay, with no state change.
    assert notes == {live, lost, filed}
    assert links == {live}
    # Only the source that only the deleted note opened goes.
    assert sources == {shared, only_live}
    assert documents == 0
    assert not any(path.exists() for path in removed_files)
    assert not any(path.exists() for path in removed_archives)
    assert all(path.exists() for path in kept_files)
    assert all(path.exists() for path in kept_archives)
    assert json.loads((state / "removed-notes-pending.json").read_text()) == {"note_ids": [gone]}
    # The keep time is gone from the settings file; nothing else in it moved.
    settings = StateStore(state).read("settings").body
    assert "retention" not in settings and settings["general"] == {"timezone": "UTC"}

    # Running it again changes nothing.
    _alembic(migrated_database, wiring, "downgrade", "0015")
    _alembic(migrated_database, wiring, "upgrade", "0016")
    async with session_factory() as session:
        assert set((await session.scalars(sa.select(Note.id))).all()) == {live, lost, filed}
    assert all(path.exists() for path in kept_archives)


async def test_a_deleted_note_whose_file_is_still_in_the_vault_becomes_an_ordinary_note_again(
    migrated_database: str,
    session_factory: async_sessionmaker[AsyncSession],
    wiring: Wiring,
):
    back = new_id()
    async with session_factory() as session, session.begin():
        session.add(_note(back, "deleted"))
    page = wiring.notes_dir / "Back.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"---\nid: {back}\n---\nBack again.\n")

    _alembic(migrated_database, wiring, "downgrade", "0015")
    _alembic(migrated_database, wiring, "upgrade", "0016")

    async with session_factory() as session:
        row = await session.get(Note, back)
    assert row is not None and row.state == "ok"


async def test_an_unreadable_vault_file_keeps_every_source(
    migrated_database: str,
    session_factory: async_sessionmaker[AsyncSession],
    wiring: Wiring,
):
    gone, only_gone = new_id(), new_id()
    async with session_factory() as session, session.begin():
        session.add(_note(gone, "deleted", only_gone))
        session.add(_source(only_gone, "one"))
        await session.flush()
        session.add(NoteSource(note_id=gone, source_id=only_gone, created_at=datetime.now(tz=UTC)))
    bundle, claim = _bundle(wiring, only_gone, "one", gone)
    hidden = wiring.notes_dir / "Hidden.md"
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_text(f"Cites {only_gone}\n")
    hidden.chmod(0)
    try:
        _alembic(migrated_database, wiring, "downgrade", "0015")
        _alembic(migrated_database, wiring, "upgrade", "0016")
    finally:
        hidden.chmod(0o644)

    async with session_factory() as session:
        assert not (await session.scalars(sa.select(Note.id))).all()
        assert set((await session.scalars(sa.select(Source.id))).all()) == {only_gone}
    assert bundle.exists() and claim.exists()
