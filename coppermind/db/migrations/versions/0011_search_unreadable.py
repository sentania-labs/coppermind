"""Remember unreadable search inputs."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "search_unreadable",
        sa.Column("kind", sa.Text(), primary_key=True),
        sa.Column("ref_id", sa.Text(), primary_key=True),
        *[
            sa.Column(name, sa.Text(), nullable=False)
            for name in ("version", "path", "observation", "reason")
        ],
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("search_unreadable")
