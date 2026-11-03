"""Queryable calendar records, rebuilt by streaming the filesystem catalog."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calendar_events",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("calendar_id", sa.Text, nullable=False),
        sa.Column("uid", sa.Text, nullable=False),
        sa.Column("semantic_hash", sa.Text, nullable=False),
        sa.Column("record", sa.Text, nullable=False),
    )
    op.create_index("ix_calendar_events_source_uid", "calendar_events", ["calendar_id", "uid"])
    op.create_table(
        "calendar_evidence",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("contact_id", sa.Text, nullable=False),
        sa.Column("record", sa.Text, nullable=False),
    )
    op.create_index("ix_calendar_evidence_contact_id", "calendar_evidence", ["contact_id"])
    op.create_table(
        "calendar_state",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("calendar_state")
    op.drop_table("calendar_evidence")
    op.drop_table("calendar_events")
