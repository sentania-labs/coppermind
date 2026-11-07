"""People and company mirrors; durable corrections remain in control files."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("record", JSONB, nullable=False),
    )
    op.create_index("ix_entities_kind_name", "entities", ["kind", "name"])
    op.drop_constraint("ck_search_documents_kind", "search_documents")
    op.create_check_constraint(
        "ck_search_documents_kind",
        "search_documents",
        "kind IN ('note', 'source', 'artifact', 'entity')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM search_documents WHERE kind = 'entity'")
    op.drop_constraint("ck_search_documents_kind", "search_documents")
    op.create_check_constraint(
        "ck_search_documents_kind", "search_documents", "kind IN ('note', 'source', 'artifact')"
    )
    op.drop_table("entities")
