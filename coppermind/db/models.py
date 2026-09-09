"""SQLAlchemy models for the mirrored note metadata."""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Index, Integer, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
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
    path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
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
