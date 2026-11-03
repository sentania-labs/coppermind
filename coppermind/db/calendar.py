"""Queryable calendar mirror. Originals and the streamed catalog remain authoritative."""

import sqlalchemy as sa

from coppermind.db.models import Base

metadata = Base.metadata
calendar_events = sa.Table(
    "calendar_events",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("calendar_id", sa.Text, nullable=False),
    sa.Column("uid", sa.Text, nullable=False),
    sa.Column("semantic_hash", sa.Text, nullable=False),
    sa.Column("record", sa.Text, nullable=False),
)
sa.Index("ix_calendar_events_source_uid", calendar_events.c.calendar_id, calendar_events.c.uid)
calendar_evidence = sa.Table(
    "calendar_evidence",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("contact_id", sa.Text, nullable=False, index=True),
    sa.Column("record", sa.Text, nullable=False),
)
calendar_state = sa.Table(
    "calendar_state",
    metadata,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("value", sa.Text, nullable=False),
)
