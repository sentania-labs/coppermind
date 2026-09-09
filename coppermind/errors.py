"""One error envelope for both HTTP contracts.

Every non-2xx answer, public or internal, is
`{"error": "<code>", "message": "<human text>", ...extra}`. The store speaks
it, the API speaks it, and `HttpStoreClient` turns it back into the typed
errors of the store contract. Defining it once is what stops the two surfaces
from drifting into two different error shapes.

This module deliberately imports no web framework: the Git helper and the
sync supervisor share the vocabulary without carrying a server dependency.
"""

from __future__ import annotations

from typing import Any, Literal

from coppermind.store_protocol import (
    MetadataUnavailable,
    NotesFilesystemUnavailable,
    NoteUnparseable,
    NotFound,
    PathCollision,
    StoreError,
    StoreUnavailable,
    ValidationFailed,
)

METADATA_UNAVAILABLE_MESSAGE = (
    "the metadata database is unavailable; a note file may already have been written and "
    "reconciliation converges the notes filesystem once it returns"
)

NOTES_FILESYSTEM_UNAVAILABLE_MESSAGE = (
    "the notes filesystem could not be read or written; this operation did not succeed and can be "
    "retried once the volume is healthy"
)

STORE_UNAVAILABLE_MESSAGE = (
    "the store could not be reached; if this was a write, its outcome is unknown and "
    "reconciliation converges the notes filesystem once the store returns"
)

# Which contract an envelope is being built for. The public surface carries no
# cause text, because it is unauthenticated in this slice and a raw operating
# system error names container paths. The internal surface keeps the cause,
# because `HttpStoreClient` reads it back to rebuild the typed error. Public is
# the default so a new caller cannot leak by forgetting to say.
Surface = Literal["public", "internal"]


# Codes for the answers a web framework raises before a route is reached, so
# both services name them the same way.
STATUS_CODES = {404: "not_found", 405: "method_not_allowed"}


def envelope(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": code, "message": message, **extra}


def code_for_status(status_code: int) -> str:
    return STATUS_CODES.get(status_code, "error")


def to_http(error: StoreError, *, surface: Surface = "public") -> tuple[int, dict[str, Any]]:
    """Map a typed store error onto its status code and error envelope."""
    cause = {"detail": str(error)} if surface == "internal" else {}
    if isinstance(error, NotFound):
        return 404, envelope("not_found", str(error), note_id=error.note_id)
    if isinstance(error, ValidationFailed):
        return 422, envelope("validation_error", str(error), errors=error.errors)
    if isinstance(error, PathCollision):
        return 409, envelope("path_collision", str(error), existing_path=error.existing_path)
    if isinstance(error, NoteUnparseable):
        return 409, envelope(
            "note_unparseable", str(error), note_id=error.note_id, reason=error.reason
        )
    if isinstance(error, MetadataUnavailable):
        return 503, envelope("metadata_unavailable", METADATA_UNAVAILABLE_MESSAGE)
    if isinstance(error, NotesFilesystemUnavailable):
        return 503, envelope(
            "notes_filesystem_unavailable", NOTES_FILESYSTEM_UNAVAILABLE_MESSAGE, **cause
        )
    if isinstance(error, StoreUnavailable):
        return 503, envelope("store_unavailable", STORE_UNAVAILABLE_MESSAGE, **cause)
    return 500, envelope("internal_error", "unexpected store error", **cause)
