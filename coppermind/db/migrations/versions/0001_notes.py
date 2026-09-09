"""Mirror table for notes.

Revision ID: 0001
Revises:
Create Date: 2026-09-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notes",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("mtime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("frontmatter", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("type", sa.Text(), nullable=True),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("account", sa.Text(), nullable=True),
        sa.Column("date", sa.Date(), nullable=True),
        sa.Column("reviewed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("state", sa.Text(), nullable=False, server_default="ok"),
        sa.Column("state_reason", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("path", name="uq_notes_path"),
    )
    op.create_index("ix_notes_reviewed_path", "notes", ["reviewed", "path"])
    op.create_index("ix_notes_type", "notes", ["type"])
    op.create_index("ix_notes_context_account", "notes", ["context", "account"])
    op.create_index("ix_notes_date", "notes", ["date"])


def downgrade() -> None:
    op.drop_index("ix_notes_date", table_name="notes")
    op.drop_index("ix_notes_context_account", table_name="notes")
    op.drop_index("ix_notes_type", table_name="notes")
    op.drop_index("ix_notes_reviewed_path", table_name="notes")
    op.drop_table("notes")
