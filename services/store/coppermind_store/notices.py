"""Durable, operator-facing notices about note paths."""

import json

from coppermind.atomicio import atomic_write_bytes


def record_path_notice(store, note_id: str, recorded_path: str, observed_path: str) -> None:
    value = {
        "note_id": note_id,
        "recorded_path": recorded_path,
        "observed_path": observed_path,
        "reason": (
            f"Note found at {observed_path}; the index recorded {recorded_path}. "
            "Coppermind followed the note identity."
        ),
    }
    atomic_write_bytes(
        store.control.store.state_dir / "path-notices" / f"{note_id}.json",
        json.dumps(value).encode(),
    )
