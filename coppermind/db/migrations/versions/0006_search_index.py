"""search_index

The full-text index of note bodies and titles, and of the generated source
projections under the sources folder. Every row is derived: the indexer
rebuilds the whole table from the notes filesystem, so dropping it loses
nothing a job cannot put back.

The number and down revision are provisional; they are assigned at merge.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# English stems the words of a note, and 'simple' keeps every word as written,
# so a name that English would stem or drop as a stop word still matches.
SEARCH_VECTOR = (
    "setweight(to_tsvector('english'::regconfig, coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('simple'::regconfig, coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english'::regconfig, coalesce(body, '')), 'B') || "
    "setweight(to_tsvector('simple'::regconfig, coalesce(body, '')), 'C')"
)


def upgrade() -> None:
    op.create_table(
        "search_documents",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ref_id", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(SEARCH_VECTOR, persisted=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("kind", "ref_id"),
        sa.CheckConstraint("kind IN ('note', 'source')", name="ck_search_documents_kind"),
    )
    op.create_index(
        "ix_search_documents_vector",
        "search_documents",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.create_table(
        "search_index_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("last_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rebuild_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rebuild_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rebuild_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 1", name="ck_search_index_state_single_row"),
    )
    op.execute("INSERT INTO search_index_state (id) VALUES (1)")


def downgrade() -> None:
    op.drop_table("search_index_state")
    op.drop_index("ix_search_documents_vector", table_name="search_documents")
    op.drop_table("search_documents")
