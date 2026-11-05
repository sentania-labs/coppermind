"""Durable, operator-facing notices about note paths.

A notice reports a path disagreement that is still open: the index recorded one
path and the note was found at another that the index has not been allowed to
adopt. It is dropped as soon as the index and the file agree, or the note is gone.
"""

import json

from coppermind.atomicio import atomic_write_bytes


def _path(store, note_id: str):
    return store.control.store.state_dir / "path-notices" / f"{note_id}.json"


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
    path = _path(store, note_id)
    try:
        if json.loads(path.read_bytes()) == value:
            return
    except (OSError, ValueError):
        pass
    atomic_write_bytes(path, json.dumps(value).encode())


def clear_path_notice(store, note_id: str) -> None:
    _path(store, note_id).unlink(missing_ok=True)
