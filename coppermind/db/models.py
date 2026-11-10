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
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import UserDefinedType


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
    account: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    date: Mapped[date_type | None] = mapped_column(Date)
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Derived from the durable filing receipt, never from a workflow payload.
    filed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    filed_source_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    # The artifacts the note cites in its frontmatter, mirrored like `source_ids`.
    artifact_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    # ok, unparsed, missing or deleted. Unparseable files are recorded, never
    # rewritten, so a person's own file is never damaged by the system.
    state: Mapped[str] = mapped_column(Text, nullable=False, default="ok")
    state_reason: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_notes_reviewed_path", "reviewed", "path"),
        Index("ix_notes_type", "type"),
        Index("ix_notes_account", "account", postgresql_using="gin"),
        Index("ix_notes_date", "date"),
        Index("ix_notes_artifact_ids", "artifact_ids", postgresql_using="gin"),
    )


class Artifact(Base):
    """One artifact, mirrored from its archive manifest and extracted record.

    The archive under `/data/artifacts/<id>` holds the immutable bytes of every
    revision, the record under `/data/state/artifacts/<id>.json` holds the
    disposable extracted text, and this row is a mirror of both that
    `rebuild_metadata` recreates from `/data` alone. `path` is the browsable
    copy under the artifacts folder of the notes filesystem, relative to its
    root, and is null for a link.
    """

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    filename: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    url: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    path: Mapped[str | None] = mapped_column(Text)
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Set when filing copied a source's artifact out of its bundle, so the
    # same source artifact always resolves to the same artifact identity.
    source_id: Mapped[str | None] = mapped_column(Text)
    source_name: Mapped[str | None] = mapped_column(Text)
    text_status: Mapped[str] = mapped_column(Text, nullable=False, default="unsupported")
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # ok, or modified when the browsable copy no longer hashes to the archive.
    state: Mapped[str] = mapped_column(Text, nullable=False, default="ok")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_artifacts_received_at", "received_at"),
        Index("ix_artifacts_sha256", "sha256"),
        Index("ix_artifacts_url", "url"),
        Index("ix_artifacts_source", "source_id", "source_name"),
        Index("ix_artifacts_tags", "tags", postgresql_using="gin"),
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
    approved_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    """The full-text index entry for one note, source projection or artifact.

    Derived, never authoritative: the indexer rebuilds every row from the notes
    filesystem and the artifact records. `kind` is `note` (keyed by the note
    identifier), `source` (a generated page under the sources folder, keyed by
    the source identifier) or `artifact` (keyed by the artifact identifier,
    its body the extracted text). `version` is what the row was indexed from,
    the note's content hash, the source revision, or the artifact revision and
    extractor version, so a pass can tell a stale row from a current one
    without reading the file.
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
        CheckConstraint(
            "kind IN ('note', 'source', 'artifact', 'entity')", name="ck_search_documents_kind"
        ),
        Index("ix_search_documents_vector", "search_vector", postgresql_using="gin"),
    )


class Vector(UserDefinedType[Any]):
    """pgvector's `vector` with no fixed dimension.

    Values travel as text (`[0.1,0.2,...]`) and are cast on the way in, so no
    driver codec is needed. A column of no fixed dimension holds whatever the
    configured embeddings model produces, and a model change needs no
    migration; distances are exact, which suits a notes filesystem's scale.
    """

    cache_ok = True

    def get_col_spec(self, **_: Any) -> str:
        return "vector"

    class comparator_factory(UserDefinedType.Comparator):  # noqa: N801 - SQLAlchemy hook name
        def cosine_distance(self, other: Any) -> Any:
            return self.op("<=>", return_type=Float())(other)


class SearchEmbedding(Base):
    """One embedded passage of a search document.

    Derived, like the document: the embeddings worker rebuilds every row from
    `search_documents` and can drop them all without losing anything. A row
    belongs to the document version and model it was made from, so an edited
    note or a changed model leaves stale rows the worker replaces. Deleting a
    document deletes its passages with it, which is how a purge or a removed
    note takes its vectors too. `start_char` and `end_char` locate the passage
    in the document body for the excerpt a hit shows.
    """

    __tablename__ = "search_embeddings"

    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    ref_id: Mapped[str] = mapped_column(Text, primary_key=True)
    chunk_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc_version: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[Any] = mapped_column(Vector(), nullable=False)
    embedded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["kind", "ref_id"],
            ["search_documents.kind", "search_documents.ref_id"],
            ondelete="CASCADE",
            name="fk_search_embeddings_document",
        ),
        Index("ix_search_embeddings_model", "model"),
    )


class SearchUnreadable(Base):
    """A failed search read at a particular version and filesystem state."""

    __tablename__ = "search_unreadable"
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    ref_id: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    observation: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SearchIndexState(Base):
    """The one row describing the index as a whole, for Admin and readiness."""

    __tablename__ = "search_index_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rebuild_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rebuild_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rebuild_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (CheckConstraint("id = 1", name="ck_search_index_state_single_row"),)


class FieldIndexGeneration(Base):
    """A complete, atomically published declared-field index generation."""

    __tablename__ = "field_index_generations"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    schema_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    definitions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        Index(
            "ix_field_generation_active", "active", unique=True, postgresql_where=sa_text("active")
        ),
    )


class NoteFieldValue(Base):
    __tablename__ = "note_field_values"
    generation: Mapped[str] = mapped_column(
        Text, ForeignKey("field_index_generations.id", ondelete="CASCADE"), primary_key=True
    )
    note_id: Mapped[str] = mapped_column(
        Text, ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True
    )
    field: Mapped[str] = mapped_column(Text, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[Any | None] = mapped_column(JSONB)
    date_value: Mapped[date_type | None] = mapped_column(Date)
    invalid_reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        Index("ix_note_fields_lookup", "generation", "field", "note_id"),
        Index("ix_note_fields_date", "generation", "field", "date_value"),
        Index(
            "ix_note_fields_value",
            "value",
            postgresql_using="gin",
            postgresql_ops={"value": "jsonb_path_ops"},
        ),
    )


class EntityRecord(Base):
    """Queryable mirror of the file-first entity catalog, including evidenced facts."""

    __tablename__ = "entities"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    record: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    __table_args__ = (Index("ix_entities_kind_name", "kind", "name"),)
