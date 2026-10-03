"""recorded_rejections

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recorded_rejections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("record_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("record_id"),
    )


def downgrade() -> None:
    op.drop_table("recorded_rejections")
