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


class VersionConflict(StoreError):
    """The note's bytes are not the ones the caller's ETag names.

    Somebody wrote the file after the caller read it: another API client, or
    a person editing on a device with the change delivered by Obsidian Sync.
    The write was refused and the file is untouched. `current_etag` is what
    the file hashes to now, so the caller can read it, merge, and retry.
    """

    def __init__(self, current_etag: ETag) -> None:
        super().__init__("the note has changed since it was read; read it again and retry")
        self.current_etag = current_etag


class PreconditionRequired(StoreError):
    """A conditional write arrived without the ETag it must be conditional on."""

    def __init__(self) -> None:
        super().__init__("an If-Match header carrying the note's current ETag is required")


class ValidationFailed(StoreError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class MetadataUnavailable(StoreError):
    """PostgreSQL is unreachable, so this operation cannot be served.

    If this was a write, its outcome is unknown when this is raised.
    Reconciliation converges the notes filesystem once PostgreSQL returns.
    """


class NoteUnparseable(StoreError):
    """The note file is on disk but its frontmatter cannot be read.

    Raised when a person edited the file on a device and left the frontmatter
    malformed. The request was fine and the store is healthy; it is the stored
    file that cannot be served, and Coppermind never rewrites it.
    """

    def __init__(self, note_id: str, reason: str) -> None:
        # The reason quotes the lines of the file the parser choked on, so it
        # is note content and never belongs in the message. It is carried as an
        # attribute for the internal surface, which the store alone answers,
        # and reaches neither the public envelope nor an operational log.
        super().__init__(
            f"the frontmatter of note {note_id} could not be parsed; "
            "Coppermind has not modified the file"
        )
        self.note_id = note_id
        self.reason = reason


class NotesFilesystemUnavailable(StoreError):
    """The notes filesystem could not be read or written.

    Raised when a file operation fails because the volume is read only, the
    disk is full, permissions deny access, or the mount is gone. PostgreSQL is
    not implicated, and `/readyz` reports the same half as not ok.
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
        return " ".join(value.split()) if isinstance(value, str) else value


class ReplaceNote(BaseModel):
    """Replace a note's frontmatter and body, keeping its identifier and path.

    This is the document shape `get_note` returns, so a caller can read a
    note, edit it and send it back whole; the fields that describe the file
    rather than its content (`id`, `path`, `title`, `content_hash` and so on)
    are ignored on the way in. The frontmatter is written exactly as sent,
    validated against the schema, with the identifier the store keeps. Both
    fields are required: a partial document is refused, never read as an
    empty body or an empty frontmatter block.
    """

    model_config = ConfigDict(extra="ignore")

    frontmatter: dict[str, Any]
    body: str


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

    async def replace_note(
        self, note_id: NoteId, request: ReplaceNote, if_match: ETag
    ) -> NoteDocument: ...


def etag_from_if_match(header: str | None) -> ETag:
    """The ETag an `If-Match` header names, or `PreconditionRequired` if none.

    The value is compared byte for byte against the note's current hash, so a
    weak validator, a list of several ETags or the `*` wildcard does not match
    anything and answers as a conflict with the current ETag. A write that is
    conditional on nothing is what this surface exists to refuse.
    """
    if header is None or not header.strip():
        raise PreconditionRequired()
    value = header.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    if not value:
        raise PreconditionRequired()
    return value
