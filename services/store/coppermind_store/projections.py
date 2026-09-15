"""Generated Markdown views of immutable source revisions."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from coppermind import frontmatter as fm
from coppermind.atomicio import atomic_write_bytes, create_exclusive_bytes
from coppermind.naming import note_stem, sanitize_folder, sanitize_stem, unique_stem
from coppermind.settings import ProductSettings
from coppermind.store_protocol import NotesFilesystemUnavailable, artifact_text
from coppermind_store.fs import NOTE_SUFFIX, existing_stems, resolve


def write_projection(
    notes_root: Path,
    settings: ProductSettings,
    *,
    source_id: str,
    revision: int,
    provider: str,
    title: str,
    note_date: date,
    generated_at: datetime,
    artifacts: list[tuple[dict[str, Any], bytes]],
    relative_path: str | None = None,
) -> tuple[str, bool]:
    """Create or replace the one projection for a source.

    `relative_path` names generated output to replace in place. Without one the
    projection is new and is assigned a collision-safe name in the configured
    sources folder; the caller decides which of the two it is, so no search of
    the sources folder happens here.
    """
    try:
        target = (
            resolve(notes_root, relative_path)
            if relative_path is not None
            else _new_path(notes_root, settings, provider, title, note_date)
        )
        data = _document(
            source_id,
            revision,
            title,
            generated_at.astimezone(ZoneInfo(settings.general.timezone)),
            artifacts,
        )
        if target.exists():
            atomic_write_bytes(target, data)
            created = False
        else:
            create_exclusive_bytes(target, data)
            created = True
        return target.relative_to(notes_root).as_posix(), created
    except NotesFilesystemUnavailable:
        raise
    except (OSError, ValueError, fm.FrontmatterError) as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc


def new_projection_path(
    notes_root: Path,
    settings: ProductSettings,
    *,
    provider: str,
    title: str,
    note_date: date,
) -> str:
    """Choose the path a source's first projection will occupy.

    The manifest is the only record of where a projection lives, so a caller
    that is about to create one names the path first and records it in the same
    manifest write that lands the revision.
    """
    try:
        target = _new_path(notes_root, settings, provider, title, note_date)
        return target.relative_to(notes_root).as_posix()
    except (OSError, ValueError) as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc


def _new_path(
    notes_root: Path,
    settings: ProductSettings,
    provider: str,
    title: str,
    note_date: date,
) -> Path:
    folder = sanitize_folder(settings.notes.sources_folder)
    provider_folder = sanitize_stem(provider.title())
    parent = resolve(notes_root, f"{folder}/{provider_folder}" if folder else provider_folder)
    stem = unique_stem(note_stem(title, note_date=note_date, dated=True), existing_stems(parent))
    return parent / f"{stem}{NOTE_SUFFIX}"


def _document(
    source_id: str,
    revision: int,
    title: str,
    generated_at: datetime,
    artifacts: list[tuple[dict[str, Any], bytes]],
) -> bytes:
    lines = [f"# {title} (source)", "", "## Artifacts", ""]
    _append_artifacts(lines, artifacts)
    frontmatter = {
        "schema_version": 1,
        "managed": True,
        "source_id": source_id,
        "source_revision": revision,
        "generated_at": generated_at.isoformat(),
    }
    return fm.compose(frontmatter, "\n".join(lines).rstrip() + "\n").encode("utf-8")


def _append_artifacts(
    lines: list[str],
    artifacts: list[tuple[dict[str, Any], bytes]],
) -> None:
    for metadata, data in artifacts:
        lines.extend([f"### {metadata['name']}", ""])
        text = artifact_text(data, str(metadata["mime_type"]))
        if text is not None:
            lines.extend([text.rstrip(), ""])
        else:
            lines.extend(
                [
                    f"Binary artifact: {metadata['mime_type']}, {metadata['size_bytes']} bytes, "
                    f"SHA-256 {metadata['sha256']}",
                    "",
                ]
            )
