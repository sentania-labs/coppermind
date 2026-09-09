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

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class NoteUnparseable(StoreError):
    """The note file is on disk but its frontmatter cannot be read.

    Raised when a person edited the file on a device and left the frontmatter
    malformed. The request was fine and the store is healthy; it is the stored
    file that cannot be served, and Coppermind never rewrites it.
    """

    def __init__(self, note_id: str, reason: str) -> None:
        super().__init__(
            f"the frontmatter of note {note_id} could not be parsed ({reason}); "
            "Coppermind has not modified the file"
        )
        self.note_id = note_id
        self.reason = reason


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

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, description="The note's H1 and filename basis.")
    body: str = ""
    frontmatter: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


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


class Store(Protocol):
    """What the API, the curator and the indexer are allowed to ask for."""

    async def create_note(self, request: CreateNote) -> NoteDocument: ...

    async def get_note(self, note_id: NoteId) -> NoteDocument: ...
