from pathlib import Path
from typing import Any, cast

import pytest
from coppermind_store.control import ControlState
from coppermind_store.notes import LocalStore

from coppermind.settings import Wiring
from coppermind.store_protocol import NotesFilesystemUnavailable, NotFound

NOTE_ID = "01K4Q8Z3N7V2X9M1B5C6D8E0F2"
OTHER_ID = "01K4Q8Z3N7V2X9M1B5C6D8E0F3"
NOTE_BYTES = b"---\nid: 01K4Q8Z3N7V2X9M1B5C6D8E0F2\nsources: []\n---\n# Runbook\n"


def local_store(tmp_path: Path) -> LocalStore:
    wiring = Wiring(data_dir=tmp_path / "data")
    control = ControlState(wiring.state_dir)
    control.ensure_defaults()
    return LocalStore(wiring.notes_dir, control, cast(Any, object()))


async def test_a_file_that_vanishes_after_location_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = local_store(tmp_path)
    missing = tmp_path / "vanished.md"

    async def locate(_: str) -> tuple[str, Path]:
        return "Review/vanished.md", missing

    monkeypatch.setattr(store, "_locate", locate)

    with pytest.raises(NotFound) as raised:
        await store.get_note(NOTE_ID)
    assert raised.value.note_id == NOTE_ID


async def test_a_file_metadata_read_failure_reports_the_notes_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = local_store(tmp_path)

    class StatFailurePath:
        def read_bytes(self) -> bytes:
            return NOTE_BYTES

        def stat(self):
            raise PermissionError("metadata read denied")

    async def locate(_: str) -> tuple[str, Path]:
        return "Review/Runbook.md", cast(Path, StatFailurePath())

    monkeypatch.setattr(store, "_locate", locate)

    with pytest.raises(NotesFilesystemUnavailable) as raised:
        await store.get_note(NOTE_ID)
    assert "metadata read denied" in str(raised.value)


async def test_a_file_carrying_another_identifier_is_never_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """One note deleted on a device and another renamed onto its path.

    Nothing reconciles the mirror yet, so the row still names that path.
    Serving what is there would answer a different note under the requested
    identifier, which is worse than a miss.
    """
    store = local_store(tmp_path)
    renamed = tmp_path / "Runbook.md"
    renamed.write_bytes(NOTE_BYTES.replace(NOTE_ID.encode(), OTHER_ID.encode()))

    async def locate(_: str) -> tuple[str, Path]:
        return "Review/Runbook.md", renamed

    monkeypatch.setattr(store, "_locate", locate)

    with pytest.raises(NotFound) as raised:
        await store.get_note(NOTE_ID)
    assert raised.value.note_id == NOTE_ID


async def test_a_file_without_an_identifier_still_reads_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A note a person created in Obsidian carries no id key of its own yet."""
    store = local_store(tmp_path)
    hand_written = tmp_path / "Runbook.md"
    hand_written.write_bytes(b"---\nsources: []\n---\n# Runbook\n")

    async def locate(_: str) -> tuple[str, Path]:
        return "Review/Runbook.md", hand_written

    monkeypatch.setattr(store, "_locate", locate)

    fetched = await store.get_note(NOTE_ID)
    assert fetched.id == NOTE_ID
    assert fetched.path == "Review/Runbook.md"
    assert fetched.body.startswith("# Runbook")
