"""Move and rename of dated notes, refused renames, and faults after the link.

These run the real store against a real notes filesystem. Only PostgreSQL is
replaced, by a small in-memory mirror that keeps rows by identifier, commits
on a clean exit from the transaction and discards a failed one, so a refused
write can be checked against the row as well as the file. The same cases run
against PostgreSQL in tests/integration/test_move_rename.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from coppermind_store import notes
from coppermind_store.control import ControlState
from coppermind_store.main import create_app
from coppermind_store.notes import LocalStore
from fastapi.testclient import TestClient
from pydantic import ValidationError

from coppermind.db.models import Note, OutboxEvent
from coppermind.settings import Wiring
from coppermind.store_protocol import (
    CreateNote,
    MoveNote,
    NotesFilesystemUnavailable,
    PathCollision,
    RenameNote,
    ValidationFailed,
)

TOKEN = "move-rename-test-token"


class Result:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalars(self) -> Result:
        return self

    def first(self) -> Any:
        return self.rows[0] if self.rows else None

    def one_or_none(self) -> Any:
        return self.rows[0] if self.rows else None


class Located:
    def __init__(self, path: str, state: str) -> None:
        self.path = path
        self.state = state


class Mirror:
    """The rows and outbox events the store has committed."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.events: list[OutboxEvent] = []

    def factory(self) -> Session:
        return Session(self)


class Session:
    def __init__(self, mirror: Mirror) -> None:
        self.mirror = mirror
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.added: list[Any] = []

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        yield
        for note_id, values in self.updates:
            self.mirror.rows[note_id].update(values)
        for row in self.added:
            if isinstance(row, Note):
                self.mirror.rows[row.id] = {
                    "path": row.path,
                    "state": row.state,
                    "content_hash": row.content_hash,
                }
            elif isinstance(row, OutboxEvent):
                self.mirror.events.append(row)

    async def execute(self, statement: Any) -> Result:
        params = statement.compile().params if hasattr(statement, "compile") else {}
        if isinstance(statement, sa.Update):
            values = {key: value for key, value in params.items() if key != "id_1"}
            self.updates.append((params["id_1"], values))
            return Result([])
        if isinstance(statement, sa.Select):
            columns = [column.name for column in statement.selected_columns]
            if columns == ["path", "state"]:
                row = self.mirror.rows.get(params["id_1"])
                return Result([Located(row["path"], row["state"])] if row else [])
            if columns == ["id"] and "path_1" in params:
                return Result(
                    [
                        note_id
                        for note_id, row in self.mirror.rows.items()
                        if row["path"] == params["path_1"] and row["state"] != "missing"
                    ]
                )
        return Result([])

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


def _store(tmp_path: Path) -> tuple[LocalStore, Mirror]:
    notes_root = tmp_path / "notes"
    notes_root.mkdir()
    sources_root = tmp_path / "sources"
    sources_root.mkdir()
    control = ControlState(tmp_path / "state")
    control.ensure_defaults()
    mirror = Mirror()
    return LocalStore(notes_root, control, cast(Any, mirror.factory), sources_root), mirror


def meeting(title: str) -> CreateNote:
    return CreateNote(
        title=title,
        body="Agenda\n",
        frontmatter={
            "date": "2026-10-02",
            "type": "meeting",
            "context": "internal",
            "reviewed": False,
            "sources": [],
            "tags": [],
        },
    )


def plain(title: str) -> CreateNote:
    return CreateNote(title=title, body="Body\n", frontmatter={"type": "note"})


def note_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def hand_written(store: LocalStore, mirror: Mirror, relative: str, heading: str) -> str:
    """A note a person named themselves, mirrored the way the reconciler would."""
    from coppermind.ids import new_id

    note_id = new_id()
    path = store.notes_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {note_id}\ndate: 2026-10-02\nschema_version: 1\ntype: note\n"
        "context: internal\nreviewed: false\nsources: []\ntags: []\n---\n"
        f"# {heading}\n\nBody\n",
        encoding="utf-8",
    )
    mirror.rows[note_id] = {"path": relative, "state": "ok"}
    return note_id


# -- 1: dated notes ---------------------------------------------------------


async def test_an_api_created_meeting_note_moves_and_renames_with_a_date_only_stem(
    tmp_path: Path,
) -> None:
    store, mirror = _store(tmp_path)
    created = await store.create_note(meeting("Architecture Sync"))
    assert created.path == "Review/2026-10-02 Architecture Sync.md"

    moved = await store.move_note(created.id, MoveNote(target_folder="Work"), None)
    assert moved.id == created.id
    assert moved.path == "Work/2026-10-02 Architecture Sync.md"

    renamed = await store.rename_note(
        created.id, RenameNote(title="Design Review"), moved.content_hash
    )
    assert renamed.id == created.id
    assert renamed.path == "Work/2026-10-02 Design Review.md"
    assert [path.name for path in note_files(store.notes_root)] == ["2026-10-02 Design Review.md"]
    assert mirror.rows[created.id]["path"] == renamed.path
    assert [event.event_type for event in mirror.events] == ["note.moved", "note.renamed"]


@pytest.mark.parametrize("written", ["2026-10-02 09:30", "2026-10-02 09:30:00"])
async def test_a_hand_written_date_with_a_time_never_puts_a_colon_in_the_filename(
    tmp_path: Path, written: str
) -> None:
    store, mirror = _store(tmp_path)
    note_id = hand_written(store, mirror, "Review/Standup.md", "Standup")
    path = store.notes_root / "Review/Standup.md"
    text = path.read_text(encoding="utf-8").replace("date: 2026-10-02\n", f"date: {written}\n")
    path.write_text(text.replace("type: note", "type: meeting"), encoding="utf-8")
    original = path.read_bytes()

    moved = await store.move_note(note_id, MoveNote(target_folder="Work"), None)
    assert moved.path == "Work/Standup.md"
    assert (store.notes_root / moved.path).read_bytes() == original

    # The schema wants a date, so a rename is refused before the file changes
    # rather than deriving a filename from the time.
    with pytest.raises(ValidationFailed):
        await store.rename_note(note_id, RenameNote(title="Daily"), notes.content_hash(original))
    assert note_files(store.notes_root) == [store.notes_root / moved.path]
    assert (store.notes_root / moved.path).read_bytes() == original


# -- 2: a refused rename leaves the source alone ----------------------------


async def test_a_rename_refused_at_the_link_leaves_the_source_bytes_and_etag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, mirror = _store(tmp_path)
    created = await store.create_note(plain("Original"))
    source = store.notes_root / created.path
    original = source.read_bytes()
    destination = store.notes_root / "Review/Renamed.md"
    real_link = notes.os.link

    def raced_link(src: Any, dst: Any) -> None:
        Path(dst).write_bytes(b"Device wins")
        real_link(src, dst)

    monkeypatch.setattr(notes.os, "link", raced_link)
    with pytest.raises(PathCollision) as refused:
        await store.rename_note(created.id, RenameNote(title="Renamed"), created.content_hash)

    assert refused.value.existing_path == "Review/Renamed.md"
    assert source.read_bytes() == original
    assert destination.read_bytes() == b"Device wins"
    assert note_files(store.notes_root) == sorted([source, destination])
    monkeypatch.setattr(notes.os, "link", real_link)
    assert (await store.get_note(created.id)).content_hash == created.content_hash
    assert mirror.rows[created.id]["path"] == created.path
    assert mirror.rows[created.id]["content_hash"] == created.content_hash
    assert mirror.events == []


# -- 3: rename title validation ---------------------------------------------


@pytest.mark.parametrize("title", ["", "   ", "Line one\n# Injected", "Carriage\rreturn", "Nul\0"])
def test_a_rename_title_that_is_empty_or_multi_line_or_has_nul_is_refused(title: str) -> None:
    with pytest.raises(ValidationError):
        RenameNote(title=title)


def test_a_rename_title_collapses_whitespace_like_create() -> None:
    assert RenameNote(title="  New \t title  ").title == "New title"


@pytest.mark.parametrize("title", ["", "Line one\n# Injected", "Nul\u0000"])
def test_a_bad_rename_title_is_422_over_the_wire_before_the_file_changes(
    tmp_path: Path, title: str
) -> None:
    token = tmp_path / "internal-token"
    token.write_text(f"{TOKEN}\n", encoding="utf-8")
    password = tmp_path / "postgres-password"
    password.write_text("coppermind\n", encoding="utf-8")
    wiring = Wiring(
        data_dir=tmp_path / "data",
        internal_token_file=token,
        database_url="postgresql://coppermind@127.0.0.1:5999/absent",
        db_password_file=password,
    )
    (tmp_path / "store").mkdir()
    store, mirror = _store(tmp_path / "store")
    note_id = hand_written(store, mirror, "Review/Keep.md", "Keep")
    path = store.notes_root / "Review/Keep.md"
    original = path.read_bytes()

    with TestClient(create_app(wiring)) as client:
        client.app.state.store = store  # type: ignore[attr-defined]
        response = client.post(
            f"/internal/v1/notes/{note_id}/rename",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "If-Match": f'"{notes.content_hash(original)}"',
            },
            json={"title": title},
        )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert path.read_bytes() == original
    assert note_files(store.notes_root) == [path]


# -- 4: move keeps the filename ---------------------------------------------


async def test_a_move_keeps_a_collision_suffix(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    await store.create_note(plain("Same"))
    second = await store.create_note(plain("Same"))
    assert second.path == "Review/Same (2).md"

    moved = await store.move_note(second.id, MoveNote(target_folder="Work"), None)
    assert moved.path == "Work/Same (2).md"
    assert (store.notes_root / "Work/Same (2).md").is_file()
    assert not (store.notes_root / "Work/Same.md").exists()


async def test_a_move_keeps_a_hand_chosen_filename(tmp_path: Path) -> None:
    store, mirror = _store(tmp_path)
    note_id = hand_written(store, mirror, "Review/standup notes.md", "Weekly")
    original = (store.notes_root / "Review/standup notes.md").read_bytes()

    moved = await store.move_note(note_id, MoveNote(target_folder="Work"), None)
    assert moved.path == "Work/standup notes.md"
    assert moved.title == "Weekly"
    assert (store.notes_root / "Work/standup notes.md").read_bytes() == original
    assert not (store.notes_root / "Work/Weekly.md").exists()
    assert mirror.rows[note_id]["path"] == "Work/standup notes.md"


async def test_a_move_into_the_current_folder_is_a_no_op(tmp_path: Path) -> None:
    store, mirror = _store(tmp_path)
    await store.create_note(plain("Same"))
    second = await store.create_note(plain("Same"))
    path = store.notes_root / second.path
    original = path.read_bytes()
    mtime = path.stat().st_mtime_ns

    moved = await store.move_note(second.id, MoveNote(target_folder="Review"), second.content_hash)
    assert moved.id == second.id
    assert moved.path == second.path
    assert moved.content_hash == second.content_hash
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == mtime
    assert mirror.events == []


# -- 5: a fault after the link -----------------------------------------------


@pytest.mark.parametrize("operation", ["move", "rename"])
async def test_a_fault_after_the_link_removes_the_new_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    store, mirror = _store(tmp_path)
    created = await store.create_note(plain("Original"))
    source = store.notes_root / created.path
    original = source.read_bytes()
    if operation == "move":
        (store.notes_root / "Work").mkdir()

    def failing_sync(directory: Path) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(notes, "sync_directory", failing_sync)
    with pytest.raises(NotesFilesystemUnavailable):
        if operation == "move":
            await store.move_note(created.id, MoveNote(target_folder="Work"), None)
        else:
            await store.rename_note(created.id, RenameNote(title="Renamed"), created.content_hash)

    assert note_files(store.notes_root) == [source]
    assert source.read_bytes() == original
    assert mirror.rows[created.id]["path"] == created.path
    assert mirror.events == []


def test_a_fault_after_the_source_is_gone_keeps_the_only_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "Review" / "Note.md"
    target = tmp_path / "Work" / "Note.md"
    source.parent.mkdir()
    source.write_bytes(b"note")
    calls: list[Path] = []

    def sync_then_fail(directory: Path) -> None:
        calls.append(directory)
        if len(calls) == 2:
            raise OSError("fsync failed")

    monkeypatch.setattr(notes, "sync_directory", sync_then_fail)
    with pytest.raises(OSError, match="fsync failed"):
        notes._move_file(source, target)
    assert not source.exists()
    assert target.read_bytes() == b"note"


# -- 6: typed failures ------------------------------------------------------


async def test_a_metadata_outage_during_a_move_is_metadata_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import OperationalError

    from coppermind.store_protocol import MetadataUnavailable

    store, _ = _store(tmp_path)
    created = await store.create_note(plain("Original"))
    real_execute = Session.execute

    async def outage(self: Session, statement: Any) -> Result:
        if isinstance(statement, sa.Select) and "path_1" in statement.compile().params:
            raise OperationalError("SELECT", {}, Exception("connection refused"))
        return await real_execute(self, statement)

    monkeypatch.setattr(Session, "execute", outage)
    with pytest.raises(MetadataUnavailable):
        await store.move_note(created.id, MoveNote(target_folder="Work"), None)
    assert note_files(store.notes_root) == [store.notes_root / created.path]


async def test_a_filesystem_failure_during_a_rename_is_a_filesystem_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _store(tmp_path)
    created = await store.create_note(plain("Original"))

    def failing_link(src: Any, dst: Any) -> None:
        raise PermissionError("read-only notes filesystem")

    monkeypatch.setattr(notes.os, "link", failing_link)
    with pytest.raises(NotesFilesystemUnavailable):
        await store.rename_note(created.id, RenameNote(title="Renamed"), created.content_hash)
    assert note_files(store.notes_root) == [store.notes_root / created.path]


# -- 7: case-insensitive folder resolution ----------------------------------


async def test_a_target_folder_takes_the_spelling_of_an_existing_folder(tmp_path: Path) -> None:
    store, mirror = _store(tmp_path)
    (store.notes_root / "Work" / "Customers").mkdir(parents=True)
    created = await store.create_note(plain("Original"))

    moved = await store.move_note(created.id, MoveNote(target_folder="work/customers/New"), None)
    assert moved.path == "Work/Customers/New/Original.md"
    assert sorted(path.name for path in store.notes_root.iterdir()) == ["Review", "Work"]
    assert mirror.rows[created.id]["path"] == moved.path
