"""Allow a missing note and its replacement to share a historical path.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-15
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_notes_path", "notes", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint("uq_notes_path", "notes", ["path"])
