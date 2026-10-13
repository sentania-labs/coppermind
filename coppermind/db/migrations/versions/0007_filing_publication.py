"""A filing receipt, rather than a folder name, authorizes publication."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notes", sa.Column("filed", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    for name in ("source_ids", "filed_source_ids"):
        op.add_column(
            "notes",
            sa.Column(
                name, sa.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")
            ),
        )
    # Old search rows may contain material that was never reviewed.
    op.execute("DELETE FROM search_documents")


def downgrade() -> None:
    op.drop_column("notes", "filed_source_ids")
    op.drop_column("notes", "source_ids")
    op.drop_column("notes", "filed")
