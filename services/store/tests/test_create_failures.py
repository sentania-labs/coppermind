from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import coppermind_store.notes as notes_module
import pytest
from coppermind_store.control import ControlState
from coppermind_store.notes import LocalStore
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from coppermind.settings import Wiring
from coppermind.store_protocol import CreateNote, MetadataUnavailable, PathCollision


class SessionWithLostCommitResult:
    async def execute(self, statement: Any) -> Any:
        return EmptyResult()

    def add(self, row: Any) -> None:
        return None


class EmptyResult:
    def scalars(self) -> "EmptyResult":
        return self

    def first(self) -> None:
        return None


@asynccontextmanager
async def transaction_with_lost_commit_result(session_factory: Any):
    yield SessionWithLostCommitResult()
    raise SQLAlchemyError("commit result is unknown")


@asynccontextmanager
async def transaction_with_programming_failure(session_factory: Any):
    yield SessionWithLostCommitResult()
    raise ProgrammingError("INSERT", {}, Exception("notes table is missing"))


async def test_an_ambiguous_commit_failure_keeps_the_authoritative_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    wiring = Wiring(data_dir=tmp_path / "data")
    control = ControlState(wiring.state_dir)
    control.ensure_defaults()
    wiring.notes_dir.mkdir(parents=True)
    store = LocalStore(wiring.notes_dir, control, cast(Any, object()), wiring.sources_dir)
    monkeypatch.setattr(notes_module, "transaction", transaction_with_lost_commit_result)

    with pytest.raises(MetadataUnavailable):
        await store.create_note(CreateNote(title="Commit result unknown"))

    files = list(wiring.notes_dir.rglob("*.md"))
    assert len(files) == 1
    assert "# Commit result unknown" in files[0].read_text(encoding="utf-8")


async def test_a_database_programming_failure_keeps_the_authoritative_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    wiring = Wiring(data_dir=tmp_path / "data")
    control = ControlState(wiring.state_dir)
    control.ensure_defaults()
    wiring.notes_dir.mkdir(parents=True)
    store = LocalStore(wiring.notes_dir, control, cast(Any, object()), wiring.sources_dir)
    monkeypatch.setattr(notes_module, "transaction", transaction_with_programming_failure)

    with pytest.raises(MetadataUnavailable):
        await store.create_note(CreateNote(title="Database schema fault"))

    files = list(wiring.notes_dir.rglob("*.md"))
    assert len(files) == 1
    assert "# Database schema fault" in files[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Concurrent-create retry tests (AC1, AC2)
# ---------------------------------------------------------------------------


class _MockSession:
    """A minimal async session that passes SELECT 1 and returns None for
    occupied-path queries, so the database check never blocks."""

    async def execute(self, statement: Any) -> "_MockResult":
        return _MockResult()

    def add(self, row: Any) -> None:
        pass


class _MockResult:
    def scalars(self) -> "_MockResult":
        return self

    def first(self) -> Any:
        return None


@asynccontextmanager
async def _mock_transaction(session_factory: Any):
    session = _MockSession()
    yield session


async def test_concurrent_create_retries_on_file_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When exclusive create fails with FileExistsError the store retries
    with a recomputed stem and succeeds, keeping the original note id."""
    wiring = Wiring(data_dir=tmp_path / "data")
    control = ControlState(wiring.state_dir)
    control.ensure_defaults()
    wiring.notes_dir.mkdir(parents=True)

    call_count = 0

    def fake_exclusive(path, data, *, mode=0o644):
        nonlocal call_count
        call_count += 1
        # First call raises FileExistsError, then the second call succeeds.
        if call_count == 1:
            # Create the file that causes the collision so existing_stems
            # sees it on the next listing.
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            raise FileExistsError(str(path))
        # Second call: write the actual file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    # The name is bound in the notes module via import, so patch it there.
    monkeypatch.setattr(notes_module, "create_exclusive_bytes", fake_exclusive)
    monkeypatch.setattr(notes_module, "transaction", _mock_transaction)

    wiring.notes_dir.mkdir(parents=True, exist_ok=True)
    store = LocalStore(wiring.notes_dir, control, cast(Any, object()), wiring.sources_dir)

    created = await store.create_note(
        CreateNote(title="Weekly sync", frontmatter={"type": "meeting"})
    )

    # meeting is a dated type, so the stem gets a date prefix. The original
    # file (2026-.. Weekly sync.md) is on disk, so the retry should pick
    # " (2)".
    assert created.path.endswith(" (2).md")
    # The concurrent create survived with its own id.
    files = sorted(wiring.notes_dir.rglob("*.md"))
    assert len(files) == 2


async def test_concurrent_create_exhausts_retries_then_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When FileExistsError happens more than five times the store gives up
    and raises PathCollision."""
    wiring = Wiring(data_dir=tmp_path / "data")
    control = ControlState(wiring.state_dir)
    control.ensure_defaults()
    wiring.notes_dir.mkdir(parents=True)

    call_count = 0

    def fake_exclusive(path, data, *, mode=0o644):
        nonlocal call_count
        call_count += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        if call_count <= 10:
            # Write the file so the stem listing always sees a collision,
            # then raise to mimic the race. Clean up afterwards, matching
            # the real create_exclusive_bytes behaviour that removes
            # incomplete files on failure.
            path.write_bytes(data)
            raise FileExistsError(str(path))
        # Never reached in this test.
        path.write_bytes(data)

    monkeypatch.setattr(notes_module, "create_exclusive_bytes", fake_exclusive)
    monkeypatch.setattr(notes_module, "transaction", _mock_transaction)

    wiring.notes_dir.mkdir(parents=True, exist_ok=True)
    store = LocalStore(wiring.notes_dir, control, cast(Any, object()), wiring.sources_dir)

    with pytest.raises(PathCollision):
        await store.create_note(CreateNote(title="Weekly sync", frontmatter={"type": "meeting"}))

    # The fake does not clean up like the real one does. After 5 retries
    # each with a file written and then an exception, the directory still
    # has those files. We verify that PathCollision was raised; file
    # cleanup is tested by the existing create_exclusive_bytes tests.
