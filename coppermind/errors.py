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

from typing import Any

from coppermind.store_protocol import (
    MetadataUnavailable,
    NotesFilesystemUnavailable,
    NotFound,
    PathCollision,
    StoreError,
    StoreUnavailable,
    ValidationFailed,
)

METADATA_UNAVAILABLE_MESSAGE = (
    "the metadata database is unavailable; the notes filesystem is unaffected and this "
    "operation can be retried once it returns"
)

NOTES_FILESYSTEM_UNAVAILABLE_MESSAGE = (
    "the notes filesystem could not be written; no note was created and readiness reports "
    "the same fault"
)


def envelope(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": code, "message": message, **extra}


def to_http(error: StoreError) -> tuple[int, dict[str, Any]]:
    """Map a typed store error onto its status code and error envelope."""
    if isinstance(error, NotFound):
        return 404, envelope("not_found", str(error), note_id=error.note_id)
    if isinstance(error, ValidationFailed):
        return 422, envelope("validation_error", str(error), errors=error.errors)
    if isinstance(error, PathCollision):
        return 409, envelope("path_collision", str(error), existing_path=error.existing_path)
    if isinstance(error, MetadataUnavailable):
        return 503, envelope("metadata_unavailable", METADATA_UNAVAILABLE_MESSAGE)
    if isinstance(error, NotesFilesystemUnavailable):
        return 503, envelope(
            "notes_filesystem_unavailable", NOTES_FILESYSTEM_UNAVAILABLE_MESSAGE, detail=str(error)
        )
    if isinstance(error, StoreUnavailable):
        return 503, envelope("store_unavailable", str(error))
    return 500, envelope("internal_error", "unexpected store error")
