"""Generated Markdown views of immutable source revisions."""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from coppermind import frontmatter as fm
from coppermind.atomicio import commit_staged_exclusive, create_exclusive_bytes, stage_bytes
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
    title: str,
    revision_ingested_at: datetime,
    artifacts: list[tuple[dict[str, Any], bytes]],
    relative_path: str,
) -> bool:
    """Write the one projection for a source at the path its caller chose.

    The manifest that records `relative_path` is the only place a projection is
    ever found again, so the caller names it with `new_projection_path` before
    anything is written and no path is chosen here. True means the file was
    created, False that generated output was replaced where it stood.
    """
    try:
        target = resolve(notes_root, relative_path)
        data = _document(
            source_id,
            revision,
            title,
            revision_ingested_at.astimezone(ZoneInfo(settings.general.timezone)),
            artifacts,
        )
        if target.exists():
            _replace_projection(notes_root, target, data, source_id, revision)
            return False
        try:
            create_exclusive_bytes(target, data)
        except FileExistsError as exc:
            raise ProjectionNotPlaced(source_id, revision, relative_path) from exc
        return True
    except NotesFilesystemUnavailable:
        raise
    except (OSError, ValueError, fm.FrontmatterError) as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc


def _replace_projection(
    notes_root: Path, target: Path, data: bytes, source_id: str, revision: int
) -> None:
    """Replace the exact file validated as this source's generated output."""
    staged = stage_bytes(target, data)
    try:
        held = _hold_current(target)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    held_is_projection = False
    try:
        relative = target.relative_to(notes_root).as_posix()
        held_relative = held.relative_to(notes_root).as_posix()
        if projection_revision(notes_root, held_relative, source_id) is None:
            _restore_if_vacant(held, target)
            raise ProjectionNotPlaced(source_id, revision, relative)
        held_is_projection = True
        try:
            commit_staged_exclusive(staged, target)
        except FileExistsError as exc:
            raise ProjectionNotPlaced(source_id, revision, relative) from exc
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    finally:
        if held.exists():
            if not target.exists() or not held_is_projection:
                _restore_if_vacant(held, target)
            elif held_is_projection:
                held.unlink(missing_ok=True)


def _hold_current(target: Path) -> Path:
    """Move the current occupant aside without replacing any person's file."""
    descriptor, name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".held"
    )
    os.close(descriptor)
    held = Path(name)
    try:
        os.replace(target, held)
    except BaseException:
        held.unlink(missing_ok=True)
        raise
    return held


def _restore_if_vacant(held: Path, target: Path) -> None:
    """Put a held occupant back only if no newer file now owns its name."""
    try:
        os.link(held, target)
    except FileExistsError:
        return
    held.unlink()


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
