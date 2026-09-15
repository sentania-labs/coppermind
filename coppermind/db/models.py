"""SQLAlchemy models for the mirrored note metadata."""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AdminSession(Base):
    """An expiring browser session. The recoverable token is cookie-only."""

    __tablename__ = "admin_sessions"

    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


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
