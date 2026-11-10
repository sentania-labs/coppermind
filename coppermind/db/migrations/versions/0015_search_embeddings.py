"""search_embeddings

Vectors for semantic search, in the same PostgreSQL as the full-text index
(pgvector). Every row is derived from `search_documents` and goes with its
document. The table is created here only where the extension is offered and may be
created, so an upgrade on a PostgreSQL without pgvector still starts and search stays
full-text. The store creates the same table on its own once the server offers the
extension, and Admin says what is missing.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    offered = bind.execute(
        sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
    ).first()
    if offered is None:
        return
    try:
        with bind.begin_nested():
            op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except sa.exc.DBAPIError:
        return
    op.execute(
        """
        CREATE TABLE search_embeddings (
            kind text NOT NULL,
            ref_id text NOT NULL,
            chunk_index integer NOT NULL,
            doc_version text NOT NULL,
            model text NOT NULL,
            start_char integer NOT NULL,
            end_char integer NOT NULL,
            embedding vector NOT NULL,
            embedded_at timestamptz NOT NULL,
            PRIMARY KEY (kind, ref_id, chunk_index),
            CONSTRAINT fk_search_embeddings_document FOREIGN KEY (kind, ref_id)
                REFERENCES search_documents (kind, ref_id) ON DELETE CASCADE
        )
        """
    )
    op.create_index("ix_search_embeddings_model", "search_embeddings", ["model"])


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS search_embeddings")
