"""The store contract.

The store is the only process that writes the notes filesystem. Everything
else, the API included, asks it. That boundary is defined once here as a typed
Protocol with pydantic request and response models, and it has two
implementations: `LocalStore` inside the store service, and `HttpStoreClient`
over the internal HTTP contract. Because both satisfy the same Protocol, the
service boundary can be tested from either side and neither can drift.

This file grows one method at a time as the slices land. Nothing is declared
here that no implementation provides.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

# `"sha256:<hex of the file bytes>"`. A move does not change it; a change to
# the bytes, including a frontmatter write back, does.
ETag = str
NoteId = str


class StoreError(Exception):
    """Base class for the typed errors the contract defines."""


class NotFound(StoreError):
    def __init__(self, note_id: str) -> None:
        super().__init__(f"no note with id {note_id}")
        self.note_id = note_id


class PathCollision(StoreError):
    def __init__(self, existing_path: str) -> None:
        super().__init__(f"path already in use: {existing_path}")
        self.existing_path = existing_path


class ValidationFailed(StoreError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class MetadataUnavailable(StoreError):
    """PostgreSQL is unreachable, so this operation cannot be served.

    The notes filesystem is untouched and fully usable while this is raised.
    Reconciliation converges anything that changed on disk once PostgreSQL
    returns.
    """


class NotesFilesystemUnavailable(StoreError):
    """The notes filesystem could not be written, so this operation failed.

    Raised when the write itself fails: the volume is read only, the disk is
    full, or the mount is gone. PostgreSQL is not implicated, and `/readyz`
    reports the same half as not ok.
    """


class StoreUnavailable(StoreError):
    """The store service itself could not be reached."""


class CreateNote(BaseModel):
    """Create a note in the notes filesystem."""

    title: str
    body: str = ""
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    # Relative to the notes filesystem root. Defaults to the review folder,
    # which is where anything that has not been read yet belongs.
    folder: str | None = None
    created_by: str = "api"


class NoteDocument(BaseModel):
    """A note as the rest of the system sees it."""

    id: NoteId
    path: str
    title: str
    frontmatter: dict[str, Any]
    body: str
    content_hash: ETag
    size_bytes: int
    updated_at: datetime
    sources: list[str] = Field(default_factory=list)


class RawNote(BaseModel):
    """The exact bytes on disk, for the indexer and for `text/markdown` reads."""

    id: NoteId
    path: str
    text: str
    content_hash: ETag


class Store(Protocol):
    """What the API, the curator and the indexer are allowed to ask for."""

    async def create_note(self, request: CreateNote) -> NoteDocument: ...

    async def get_note(self, note_id: NoteId) -> NoteDocument: ...

    async def read_raw(self, note_id: NoteId) -> RawNote: ...
