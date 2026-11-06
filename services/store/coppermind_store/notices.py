"""Durable, operator-facing notices about note paths.

A path notice reports a disagreement that is still open: the index recorded one
path and the note was found at another that the index has not been allowed to
adopt. An unreadable-note notice reports a listed note whose file cannot be read.
Each is dropped as soon as the disagreement or the fault is gone.
"""

import json

from coppermind.atomicio import atomic_write_bytes
from coppermind.store_protocol import NotesFilesystemUnavailable, NoteUnparseable, NotFound
from coppermind_store.fs import is_note_file, resolve


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


def open_path_notices(store, row_paths: dict[str, str]) -> list[dict]:
    """The notices whose disagreement still exists; every other notice is dropped.

    `row_paths` maps each live note to the path its row records.
    """
    still_open: list[dict] = []
    for path in sorted((store.control.store.state_dir / "path-notices").glob("*.json")):
        try:
            notice = json.loads(path.read_bytes())
            recorded, observed = notice["recorded_path"], notice["observed_path"]
            current = (
                row_paths.get(notice["note_id"]) == recorded
                and is_note_file(resolve(store.notes_root, observed))
                and not is_note_file(resolve(store.notes_root, recorded))
            )
        except (OSError, ValueError, KeyError, TypeError, NotesFilesystemUnavailable):
            continue
        if current:
            still_open.append(notice)
        else:
            path.unlink(missing_ok=True)
    return still_open


def _unreadable_path(store, note_id: str):
    return store.control.store.state_dir / "unreadable-notes" / f"{note_id}.json"


def record_unreadable_notice(store, note_id: str, note_path: str) -> None:
    value = {
        "note_id": note_id,
        "path": note_path,
        "reason": (
            "The note's file could not be read, so it is left out of the note list. "
            "Check the file's permissions on the notes filesystem; "
            "it returns to the list once it can be read."
        ),
    }
    path = _unreadable_path(store, note_id)
    try:
        if json.loads(path.read_bytes()) == value:
            return
    except (OSError, ValueError):
        pass
    atomic_write_bytes(path, json.dumps(value).encode())


async def open_unreadable_notices(store) -> list[dict]:
    """The notices whose file still cannot be read; every other notice is dropped."""
    still_open: list[dict] = []
    for path in sorted((store.control.store.state_dir / "unreadable-notes").glob("*.json")):
        try:
            notice = json.loads(path.read_bytes())
            note_id = notice["note_id"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        try:
            await store._read_current(note_id)
        except NotesFilesystemUnavailable:
            still_open.append(notice)
            continue
        except (NotFound, NoteUnparseable):
            pass
        path.unlink(missing_ok=True)
    return still_open
