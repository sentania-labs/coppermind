from pathlib import Path
from typing import Any, cast

import pytest

from coppermind.settings import Wiring
from coppermind.store_protocol import NotesFilesystemUnavailable, NotFound
from coppermind_store.control import ControlState
from coppermind_store.notes import LocalStore

NOTE_ID = "01K4Q8Z3N7V2X9M1B5C6D8E0F2"
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
