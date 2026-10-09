"""SQLAlchemy models for the mirrored note metadata."""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Note(Base):
    """One note file, mirrored from the notes filesystem.

    `id` is the identifier written into the file's frontmatter, so a row can
    always be rebuilt from the file and never the other way round. `path` is
    relative to the notes filesystem root.
    """

    __tablename__ = "notes"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    # Missing rows retain their last known path. A different note can later
    # move onto that path, so identity is unique while historical paths are not.
    path: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    mtime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    frontmatter: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    type: Mapped[str | None] = mapped_column(Text)
    context: Mapped[str | None] = mapped_column(Text)
    account: Mapped[str | None] = mapped_column(Text)
    date: Mapped[date_type | None] = mapped_column(Date)
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    # ok, unparsed or missing. A file Coppermind cannot parse is recorded, never
    # rewritten, so a person's own file is never damaged by the system.
    state: Mapped[str] = mapped_column(Text, nullable=False, default="ok")
    state_reason: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_notes_reviewed_path", "reviewed", "path"),
        Index("ix_notes_type", "type"),
        Index("ix_notes_context_account", "context", "account"),
        Index("ix_notes_date", "date"),
    )


class OutboxEvent(Base):
    """A transactional notification derived from mirrored filesystem state."""

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    note_id: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Source(Base):
    """An immutable source identity mirrored from its manifest."""

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    external_source_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False, default="")
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_identity: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("provider", "external_source_id", name="uq_sources_provider_external_id"),
    )


class SourceRevision(Base):
    __tablename__ = "source_revisions"

    source_id: Mapped[str] = mapped_column(
        Text, ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_identity: Mapped[str] = mapped_column(Text, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )


class SourceArtifact(Base):
    __tablename__ = "source_artifacts"

    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "revision"],
            ["source_revisions.source_id", "source_revisions.revision"],
            ondelete="CASCADE",
        ),
    )


class NoteSource(Base):
    __tablename__ = "note_sources"

    note_id: Mapped[str] = mapped_column(
        Text, ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(
        Text, ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RecordedRejection(Base):
    """A problem computed by the store but not written to the note."""

    __tablename__ = "recorded_rejections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    # The entity this problem is about. 'note', 'source', or 'ingest'.
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # The note ID, source ID, or ingest claim path.
    reference: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# English stems the words of a note, and 'simple' keeps every word as written,
# so a name English would stem or drop as a stop word still matches. The
# migration that creates the column carries the same expression.
SEARCH_VECTOR = (
    "setweight(to_tsvector('english'::regconfig, coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('simple'::regconfig, coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english'::regconfig, coalesce(body, '')), 'B') || "
    "setweight(to_tsvector('simple'::regconfig, coalesce(body, '')), 'C')"
)


class SearchDocument(Base):
    """The full-text index entry for one note or one source projection.

    Derived, never authoritative: the indexer rebuilds every row from the notes
    filesystem. `kind` is `note` (keyed by the note identifier) or `source`
    (a generated page under the sources folder, keyed by the source
    identifier). `version` is what the row was indexed from, the note's content
    hash or the source revision, so a pass can tell a stale row from a current
    one without reading the file.
    """

    __tablename__ = "search_documents"

    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    ref_id: Mapped[str] = mapped_column(Text, primary_key=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR, Computed(SEARCH_VECTOR, persisted=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint("kind IN ('note', 'source')", name="ck_search_documents_kind"),
        Index("ix_search_documents_vector", "search_vector", postgresql_using="gin"),
    )


class SearchIndexState(Base):
    """The one row describing the index as a whole, for Admin and readiness."""

    __tablename__ = "search_index_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rebuild_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rebuild_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rebuild_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (CheckConstraint("id = 1", name="ck_search_index_state_single_row"),)
