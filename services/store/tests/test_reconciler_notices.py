"""The notice file keeps every writer's notices when two writers meet."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from coppermind_store.control import ControlState

NOTE_A = "01K4Q8Z3N7V2X9M1B5C6D8E0FA"
NOTE_B = "01K4Q8Z3N7V2X9M1B5C6D8E0FB"
NOTE_C = "01K4Q8Z3N7V2X9M1B5C6D8E0FC"


def record(control: ControlState, note_id: str) -> None:
    control.record_reconciler_notice(
        note_id=note_id,
        title=f"Note {note_id[-1]}",
        from_path="Elsewhere/note.md",
        path="Notes/2026/2026-04/note.md",
        folder="Notes/2026/2026-04",
        reason="Moved back.",
        at=datetime(2026, 11, 5, tzinfo=UTC),
    )


def interleave(control: ControlState, rival: str) -> None:
    """Make another writer land between the next writer's read and its write."""
    write = control.store.write
    pending = [True]

    def racing(name, body, *, if_revision):
        if pending[0]:
            pending[0] = False
            other = ControlState(control.store.state_dir)
            record(other, rival)
        return write(name, body, if_revision=if_revision)

    control.store.write = racing


@pytest.mark.parametrize("existing", [False, True], ids=["file-not-yet-written", "file-present"])
def test_a_recorded_notice_does_not_overwrite_a_writer_that_landed_first(
    tmp_path: Path, existing: bool
):
    control = ControlState(tmp_path)
    if existing:
        record(control, NOTE_A)
    interleave(control, NOTE_B)

    record(control, NOTE_C)

    assert set(control.read_reconciler_notices()) == (
        {NOTE_A, NOTE_B, NOTE_C} if existing else {NOTE_B, NOTE_C}
    )


def test_a_dismissal_does_not_overwrite_a_writer_that_landed_first(tmp_path: Path):
    control = ControlState(tmp_path)
    record(control, NOTE_A)
    interleave(control, NOTE_B)

    control.clear_reconciler_notice(NOTE_A)

    assert set(control.read_reconciler_notices()) == {NOTE_B}


def test_dismissing_a_notice_that_is_not_there_writes_nothing(tmp_path: Path):
    control = ControlState(tmp_path)
    control.clear_reconciler_notice(NOTE_A)
    assert not control.store.path_for("reconciler_notices").exists()
    record(control, NOTE_A)
    revision = control.store.read("reconciler_notices").revision
    control.clear_reconciler_notice(NOTE_B)
    assert control.store.read("reconciler_notices").revision == revision
