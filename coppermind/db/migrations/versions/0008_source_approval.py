"""Bind each published source association to its approved revision."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("note_sources", sa.Column("approved_revision", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("note_sources", "approved_revision")
