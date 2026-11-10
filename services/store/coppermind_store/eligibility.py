"""Search publication requires review and completed filing."""

from __future__ import annotations

from datetime import date

import sqlalchemy as sa

from coppermind.db.models import Artifact, Note, NoteSource, Source
from coppermind.naming import sanitize_folder, sanitize_stem

MONTH_PATH = r"^[0-9]{4}-(0[1-9]|1[0-2])/"
NOTES_FOLDER_DEFAULT = "Notes"


class FilingRoots:
    """Resolve a type to a portable root, preserving an existing spelling.

    Settings, configuration and the existing top-level folders are read once,
    so resolving many notes costs no further disk or configuration reads. When
    several folders already differ only by case, an all-lowercase spelling wins,
    then plain codepoint order.
    """

    def __init__(self, settings, notes_root, dropbox_folder="Dropbox"):
        self.fallback = sanitize_folder(settings.notes.notes_folder) or NOTES_FOLDER_DEFAULT
        owned = [
            "Review",
            "People",
            "Companies",
            "Artifacts",
            "_Sources",
            "_Trash",
            "_Attachments",
            "Dropbox",
            settings.notes.review_folder,
            settings.notes.trash_folder,
            settings.notes.sources_folder,
            settings.notes.artifacts_folder,
            settings.notes.attachments_folder,
            dropbox_folder,
            *settings.reconcile.excluded_folders,
        ]
        self.reserved = {sanitize_folder(name).split("/")[0].casefold() for name in owned}
        try:
            self.existing = sorted(
                (p.name for p in notes_root.iterdir() if p.is_dir()),
                key=lambda name: (name != name.casefold(), name),
            )
        except FileNotFoundError:
            self.existing = []

    def resolve(self, value):
        if isinstance(value, list):
            value = next((item for item in value if isinstance(item, str)), None)
        if not isinstance(value, str):
            return self.fallback, None
        candidate = sanitize_stem(value, fallback="")
        if not candidate:
            return self.fallback, None
        if candidate.casefold() in self.reserved:
            return self.fallback, candidate
        matches = [name for name in self.existing if name.casefold() == candidate.casefold()]
        return (matches[0] if matches else candidate), None


def rendered_note_folder(root: str, day: date) -> str:
    return f"{root}/{day:%Y}/{day:%Y-%m}"


def reviewed_note(notes_folder: str):
    # Keep the old roots searchable until their filing pass has drained.
    new_path_pattern = r"^.+/[0-9]{4}/[0-9]{4}-(0[1-9]|1[0-2])/"
    dated_suffix = sa.literal("/") + sa.func.to_char(Note.date, "YYYY/YYYY-MM") + "/"
    new_shape = sa.and_(
        Note.path.op("~")(new_path_pattern),
        sa.func.strpos(Note.path, dated_suffix) > 0,
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
    # An artifact is indexed when it is stored: nothing about it is a human's
    # unreviewed draft, so the only gate is that its mirror row still exists.
    artifacts = sa.exists(sa.select(Artifact.id).where(Artifact.id == ref_id)).correlate_except(
        Artifact
    )
    return sa.or_(
        sa.and_(kind == "note", notes),
        sa.and_(kind == "source", sources),
        sa.and_(kind == "artifact", artifacts),
    )


def current_source(ref_id, version):
    return sa.exists(
        sa.select(Source.id).where(
            Source.id == ref_id, version == sa.func.concat("r", Source.current_revision)
        )
    ).correlate_except(Source)
