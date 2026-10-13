"""Search publication requires review and completed monthly filing."""

import sqlalchemy as sa

from coppermind.db.models import Note, NoteSource, Source

MONTH_PATH = r"^[0-9]{4}-(0[1-9]|1[0-2])/"


def reviewed_note():
    return sa.and_(
        Note.reviewed.is_(True),
        Note.filed.is_(True),
        Note.source_ids == Note.filed_source_ids,
        Note.state != "missing",
        Note.path.op("~")(MONTH_PATH),
        sa.func.starts_with(Note.path, sa.func.to_char(Note.date, "YYYY-MM") + "/"),
    )


def eligible(kind, ref_id):
    notes = sa.exists(
        sa.select(Note.id).where(Note.id == ref_id, reviewed_note())
    ).correlate_except(Note)
    sources = sa.exists(
        sa.select(Note.id)
        .join(NoteSource, NoteSource.note_id == Note.id)
        .join(Source, Source.id == NoteSource.source_id)
        .where(
            NoteSource.source_id == ref_id,
            NoteSource.approved_revision == Source.current_revision,
            reviewed_note(),
        )
    ).correlate_except(Note, NoteSource, Source)
    return sa.or_(sa.and_(kind == "note", notes), sa.and_(kind == "source", sources))


def current_source(ref_id, version):
    return sa.exists(
        sa.select(Source.id).where(
            Source.id == ref_id, version == sa.func.concat("r", Source.current_revision)
        )
    ).correlate_except(Source)
