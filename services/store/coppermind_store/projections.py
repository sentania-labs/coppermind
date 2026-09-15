"""Generated Markdown views of immutable source revisions."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from coppermind import frontmatter as fm
from coppermind.atomicio import commit_staged, create_exclusive_bytes, stage_bytes
from coppermind.naming import note_stem, sanitize_folder, sanitize_stem, unique_stem
from coppermind.settings import ProductSettings
from coppermind.store_protocol import (
    NotesFilesystemUnavailable,
    ProjectionNotPlaced,
    artifact_text,
)
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
    revision_ingested_at: datetime,
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
            revision_ingested_at.astimezone(ZoneInfo(settings.general.timezone)),
            artifacts,
        )
        if target.exists():
            _replace_projection(notes_root, target, data, source_id, revision)
            created = False
        else:
            create_exclusive_bytes(target, data)
            created = True
        return target.relative_to(notes_root).as_posix(), created
    except NotesFilesystemUnavailable:
        raise
    except (OSError, ValueError, fm.FrontmatterError) as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc


def _replace_projection(
    notes_root: Path, target: Path, data: bytes, source_id: str, revision: int
) -> None:
    """Rename `data` over generated output only while it is still this source's.

    The new bytes are staged and made durable first, so what stands between the
    last check and the rename is the check itself. Obsidian Sync can deliver one
    of the captain's own notes onto this path at any moment, and a file this
    store did not generate for this source is never written over.
    """
    staged = stage_bytes(target, data)
    try:
        relative = target.relative_to(notes_root).as_posix()
        if projection_revision(notes_root, relative, source_id) is None:
            raise ProjectionNotPlaced(source_id, revision, relative)
        commit_staged(staged, target)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def projection_revision(notes_root: Path, relative: str, source_id: str) -> int | None:
    """The revision the file at this path projects for this source.

    None means the path holds something else: a projection of another source,
    one of the captain's own notes that has come to occupy it, or nothing at
    all. Generated output is only ever replaced where it is found, so a path
    that does not answer for this source is never written over.
    """
    if not relative:
        return None
    try:
        frontmatter, _ = fm.parse(resolve(notes_root, relative).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, fm.FrontmatterError):
        return None
    except OSError as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc
    if frontmatter.get("managed") is not True or frontmatter.get("source_id") != source_id:
        return None
    revision = frontmatter.get("source_revision")
    return revision if isinstance(revision, int) else None


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
    revision_ingested_at: datetime,
    artifacts: list[tuple[dict[str, Any], bytes]],
) -> bytes:
    lines = [f"# {title} (source)", "", "## Artifacts", ""]
    _append_artifacts(lines, artifacts)
    frontmatter = {
        "schema_version": 1,
        "managed": True,
        "source_id": source_id,
        "source_revision": revision,
        "revision_ingested_at": revision_ingested_at.isoformat(),
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
