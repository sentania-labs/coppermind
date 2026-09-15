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

import base64
import binascii
from datetime import datetime
from pathlib import PurePath
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from coppermind.api_keys import ApiKeySet

# `"sha256:<hex of the file bytes>"`. A move does not change it; a change to
# the bytes, including a frontmatter write back, does.
ETag = str
NoteId = str
SourceId = str


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


class SourceClaimMissing(StoreError):
    """The durable external-id claim is gone while its mirror row survives.

    The filesystem is the truth and it no longer claims this identifier, but
    the database still holds the source it named, so ingesting again would
    make a second source for one external identifier. Nothing was written.
    Restoring `/data/sources` from a snapshot without restoring PostgreSQL, or
    removing the claim file by hand, is what produces this.
    """

    def __init__(self, provider: str, external_source_id: str) -> None:
        super().__init__(
            f"the durable claim for source {provider}/{external_source_id} is missing while its "
            "database row survives, so nothing was written; restore /data/sources and the "
            "database from the same point in time, or remove the stale row, then retry"
        )
        self.provider = provider
        self.external_source_id = external_source_id


class IncompleteRevision(StoreError):
    """A revision directory is on disk that the manifest does not record.

    An earlier revision write was interrupted between creating the directory
    and recording the revision, so the bundle holds files nothing references.
    The volume is healthy. Nothing was written and nothing was removed,
    because deciding whether those files matter is the operator's call.
    """

    def __init__(self, path: str) -> None:
        super().__init__(
            f"the source bundle holds the revision directory {path}, which its manifest does not "
            "record, so an earlier revision write was interrupted; nothing was written and "
            "nothing was removed. Inspect that directory, remove it, then retry"
        )
        self.path = path


class PayloadTooLarge(StoreError):
    def __init__(self, limit_bytes: int) -> None:
        super().__init__(f"the ingest payload exceeds the {limit_bytes} byte limit")
        self.limit_bytes = limit_bytes


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


class SourcesFilesystemUnavailable(StoreError):
    """The source bundle filesystem could not be written."""


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


class IngestArtifact(BaseModel):
    """One immutable file in a source revision."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    mime_type: str = Field(min_length=1)
    content: str | None = None
    content_base64: str | None = None

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        if value in {".", ".."} or PurePath(value).name != value or "\\" in value or "\0" in value:
            raise ValueError("must be a filename, not a path")
        return value

    @model_validator(mode="after")
    def one_content_form(self) -> IngestArtifact:
        if (self.content is None) == (self.content_base64 is None):
            raise ValueError("exactly one of content or content_base64 is required")
        if self.content_base64 is not None:
            try:
                base64.b64decode(self.content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("content_base64 is not valid base64") from exc
        return self

    def bytes(self) -> bytes:
        if self.content is not None:
            return self.content.encode("utf-8")
        return base64.b64decode(self.content_base64 or "", validate=True)


class IngestSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    external_source_id: str = Field(min_length=1)
    source_type: Literal["transcript", "document", "image", "email", "other"]
    origin: str = ""
    captured_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[IngestArtifact] = Field(min_length=1)

    @field_validator("provider", "external_source_id")
    @classmethod
    def trimmed_identity(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("captured_at")
    @classmethod
    def captured_at_has_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("must include a timezone")
        return value

    @model_validator(mode="after")
    def artifact_names_are_unique(self) -> IngestSource:
        names = [artifact.name for artifact in self.artifacts]
        if len(names) != len(set(names)):
            raise ValueError("artifact names must be unique")
        return self


class IngestNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    body: str = ""
    frontmatter: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: Any) -> Any:
        return " ".join(value.split()) if isinstance(value, str) else value


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: IngestSource
    note: IngestNote


class CreatedSource(BaseModel):
    """The source this ingest created, revised or replayed.

    `unstored_fields` names what the request sent differently from what is
    stored and this increment does not keep. A source is identified by its
    artifact bytes alone, so a correction to `captured_at`, `metadata`,
    `source_type`, `origin` or an artifact `mime_type` with the bytes
    unchanged has nowhere to land yet. Naming it is what keeps the answer
    honest instead of discarding the correction in silence. It is empty on
    every other answer.

    It covers the fields describing the source and nothing else. An ingest
    that does not create the note ignores the whole `note` object of the
    request, title, body and frontmatter alike, because the note is the
    captain's once it exists and Coppermind does not write over his edits.
    So `unstored_fields: []` beside `note.created: false` means the stored
    source matches what was sent; it says nothing about the note payload,
    which was not used at all.
    """

    id: SourceId
    revision: int = Field(ge=1)
    created: bool
    unstored_fields: list[str] = Field(default_factory=list)


class CreatedNote(BaseModel):
    id: NoteId
    path: str
    created: bool


class IngestResult(BaseModel):
    source: CreatedSource
    note: CreatedNote


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


class PatchFrontmatter(BaseModel):
    """Set and unset only named fields in a note's frontmatter."""

    model_config = ConfigDict(extra="forbid")

    set: dict[str, Any] = Field(default_factory=dict)
    unset: list[str] = Field(default_factory=list)


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

    async def patch_frontmatter(
        self, note_id: NoteId, request: PatchFrontmatter, if_match: ETag
    ) -> NoteDocument: ...

    async def ingest(
        self, request: IngestRequest, *, payload_size_bytes: int | None = None
    ) -> IngestResult: ...

    async def get_api_keys(self) -> ApiKeySet: ...


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
