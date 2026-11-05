"""Artifacts: a second first-class kind beside notes, mirrored from /data."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False, server_default=""),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("filename", sa.Text()),
        sa.Column("mime_type", sa.Text()),
        sa.Column("sha256", sa.Text()),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("url", sa.Text()),
        sa.Column("tags", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("path", sa.Text()),
        sa.Column("current_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_id", sa.Text()),
        sa.Column("source_name", sa.Text()),
        sa.Column("text_status", sa.Text(), nullable=False, server_default="unsupported"),
        sa.Column("extractor_version", sa.Text(), nullable=False, server_default=""),
        sa.Column("state", sa.Text(), nullable=False, server_default="ok"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_artifacts_received_at", "artifacts", ["received_at"])
    op.create_index("ix_artifacts_sha256", "artifacts", ["sha256"])
    op.create_index("ix_artifacts_url", "artifacts", ["url"])
    op.create_index("ix_artifacts_source", "artifacts", ["source_id", "source_name"])
    op.create_index("ix_artifacts_tags", "artifacts", ["tags"], postgresql_using="gin")
    op.add_column(
        "notes",
        sa.Column("artifact_ids", ARRAY(sa.Text()), nullable=False, server_default="{}"),
    )
    op.create_index("ix_notes_artifact_ids", "notes", ["artifact_ids"], postgresql_using="gin")
    op.drop_constraint("ck_search_documents_kind", "search_documents", type_="check")
    op.create_check_constraint(
        "ck_search_documents_kind",
        "search_documents",
        "kind IN ('note', 'source', 'artifact')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM search_documents WHERE kind = 'artifact'")
    op.execute("DELETE FROM search_unreadable WHERE kind = 'artifact'")
    op.drop_constraint("ck_search_documents_kind", "search_documents", type_="check")
    op.create_check_constraint(
        "ck_search_documents_kind", "search_documents", "kind IN ('note', 'source')"
    )
    op.drop_index("ix_notes_artifact_ids", table_name="notes")
    op.drop_column("notes", "artifact_ids")
    op.drop_index("ix_artifacts_tags", table_name="artifacts")
    op.drop_index("ix_artifacts_source", table_name="artifacts")
    op.drop_index("ix_artifacts_url", table_name="artifacts")
    op.drop_index("ix_artifacts_sha256", table_name="artifacts")
    op.drop_index("ix_artifacts_received_at", table_name="artifacts")
    op.drop_table("artifacts")
