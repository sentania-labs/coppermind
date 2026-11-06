"""Search publication requires review and completed filing."""

from __future__ import annotations

import re
from datetime import date

import sqlalchemy as sa

from coppermind.db.models import Note, NoteSource, Source
from coppermind.naming import sanitize_folder

MONTH_PATH = r"^[0-9]{4}-(0[1-9]|1[0-2])/"
NOTES_FOLDER_DEFAULT = "Notes"


def rendered_note_folder(notes_folder: str, day: date) -> str:
    root = sanitize_folder(notes_folder) or NOTES_FOLDER_DEFAULT
    return f"{root}/{day:%Y}/{day:%Y-%m}"


def awaiting_refile(notes_folder: str):
    """Reviewed notes whose path is not under the folder their date renders to."""
    root = sanitize_folder(notes_folder) or NOTES_FOLDER_DEFAULT
    prefix = sa.literal(f"{root}/") + sa.func.to_char(Note.date, "YYYY/YYYY-MM") + "/"
    return sa.and_(
        Note.reviewed.is_(True),
        Note.date.is_not(None),
        Note.state == "ok",
        ~sa.func.starts_with(Note.path, prefix),
    )


def reviewed_note(notes_folder: str):
    root = sanitize_folder(notes_folder) or NOTES_FOLDER_DEFAULT
    new_path_pattern = rf"^{re.escape(root)}/[0-9]{{4}}/[0-9]{{4}}-(0[1-9]|1[0-2])/"
    new_prefix = sa.literal(f"{root}/") + sa.func.to_char(Note.date, "YYYY/YYYY-MM") + "/"
    new_shape = sa.and_(
        Note.path.op("~")(new_path_pattern),
        sa.func.starts_with(Note.path, new_prefix),
    )
    legacy_shape = sa.and_(
        Note.path.op("~")(MONTH_PATH),
        sa.func.starts_with(Note.path, sa.func.to_char(Note.date, "YYYY-MM") + "/"),
    )
    return sa.and_(
        Note.reviewed.is_(True),
        Note.filed.is_(True),
        Note.source_ids == Note.filed_source_ids,
        Note.state.not_in(("missing", "deleted")),
        sa.or_(new_shape, legacy_shape),
    )


def eligible(kind, ref_id, notes_folder: str):
    notes = sa.exists(
        sa.select(Note.id).where(Note.id == ref_id, reviewed_note(notes_folder))
    ).correlate_except(Note)
    sources = sa.exists(
        sa.select(Note.id)
        .join(NoteSource, NoteSource.note_id == Note.id)
        .join(Source, Source.id == NoteSource.source_id)
        .where(
            NoteSource.source_id == ref_id,
            NoteSource.approved_revision == Source.current_revision,
            reviewed_note(notes_folder),
        )
    ).correlate_except(Note, NoteSource, Source)
    return sa.or_(sa.and_(kind == "note", notes), sa.and_(kind == "source", sources))


def current_source(ref_id, version):
    return sa.exists(
        sa.select(Source.id).where(
            Source.id == ref_id, version == sa.func.concat("r", Source.current_revision)
        )
    ).correlate_except(Source)
