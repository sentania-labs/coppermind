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
    # This revision deliberately permits a missing historical row and its live
    # replacement to share a path. The mirror is rebuildable, so retain the
    # live row, or the newest row when no live one exists, before restoring the
    # old uniqueness rule.
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                row_number() OVER (
                    PARTITION BY path
                    ORDER BY
                        CASE WHEN state = 'missing' THEN 1 ELSE 0 END,
                        updated_at DESC,
                        id
                ) AS path_rank
            FROM notes
        )
        DELETE FROM notes
        USING ranked
        WHERE notes.id = ranked.id
          AND ranked.path_rank > 1
        """
    )
    op.create_unique_constraint("uq_notes_path", "notes", ["path"])
