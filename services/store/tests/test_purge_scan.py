"""A purge plan inventories recovery files once for all deleted notes."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from coppermind_store import note_history, purge
from coppermind_store.control import ControlState
from coppermind_store.notes import LocalStore

from coppermind.db.models import Note
from coppermind.settings import Wiring


def test_deleted_note_receipts_use_one_directory_inventory(tmp_path: Path, monkeypatch):
    wiring = Wiring(data_dir=tmp_path)
    control = ControlState(wiring.state_dir)
    control.ensure_defaults()
    store = LocalStore(wiring.notes_dir, control, cast(Any, None), wiring.sources_dir)
    directory = note_history.directory(store)
    directory.mkdir(parents=True)
    (directory / "observed").mkdir()
    (wiring.state_dir / "filing").mkdir()
    cutoff = datetime.now(UTC)
    notes = [
        Note(
            id=f"{i:026d}",
            path=f"{i}.md",
            state="deleted",
            source_ids=[],
            updated_at=cutoff - timedelta(days=1),
            content_hash="hash",
        )
        for i in range(12)
    ]
    for note in notes:
        for commit in ("a", "b"):
            (directory / f"{note.id}-{commit}.json").write_text("{}")
    observed = directory / "observed" / f"{notes[0].id}.json"
    observed.write_text("{}")
    filed = wiring.state_dir / "filing" / f"{notes[0].id}.json"
    filed.write_text("{}")
    (directory / "unrelated.json").write_text("{}")
    scans = 0
    original = os.scandir

    def scandir(path):
        nonlocal scans
        if Path(path) == directory:
            scans += 1
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    items, _ = purge._plan(
        store, cutoff, set(), set(), {}, {}, {}, set(), notes, [], set(), [], set()
    )
    assert len(items) == len(notes)
    for item, note in zip(items, notes, strict=True):
        expected = {f"note-recovery/{note.id}-{commit}.json" for commit in ("a", "b")}
        if note is notes[0]:
            expected |= {f"note-recovery/observed/{note.id}.json", f"filing/{note.id}.json"}
        assert {entry["path"] for entry in item["files"]} == expected
    assert scans == 1
