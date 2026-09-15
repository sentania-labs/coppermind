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
from coppermind.store_protocol import NotesFilesystemUnavailable, is_text_mime
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

    Existing generated output is replaced in place. A missing projection is
    assigned a collision-safe name in the configured sources folder.
    """
    try:
        matches = _find(notes_root, settings, source_id) if relative_path is None else []
        if len(matches) > 1:
            raise NotesFilesystemUnavailable(
                f"more than one generated projection names source {source_id}"
            )
        if relative_path is not None:
            target = resolve(notes_root, relative_path)
        else:
            target = (
                matches[0]
                if matches
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


def find_projection(notes_root: Path, settings: ProductSettings, source_id: str) -> Path:
    matches = _find(notes_root, settings, source_id)
    if not matches:
        raise FileNotFoundError(source_id)
    if len(matches) > 1:
        raise NotesFilesystemUnavailable(
            f"more than one generated projection names source {source_id}"
        )
    return matches[0]


def _find(notes_root: Path, settings: ProductSettings, source_id: str) -> list[Path]:
    folder = sanitize_folder(settings.notes.sources_folder)
    root = resolve(notes_root, folder)
    if not root.exists():
        return []
    matches = []
    for path in root.rglob(f"*{NOTE_SUFFIX}"):
        try:
            frontmatter, _ = fm.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, fm.FrontmatterError):
            continue
        if frontmatter.get("managed") is True and frontmatter.get("source_id") == source_id:
            matches.append(path)
    return matches


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
    summaries = [item for item in artifacts if "summary" in str(item[0]["name"]).casefold()]
    remainder = [item for item in artifacts if item not in summaries]
    lines = [f"# {title} (source)", ""]
    if summaries:
        lines.extend(["## Summary", ""])
        _append_artifacts(lines, summaries)
    lines.extend(["## Transcript", ""])
    _append_artifacts(lines, remainder)
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
        if is_text_mime(str(metadata["mime_type"])):
            lines.extend([data.decode("utf-8").rstrip(), ""])
        else:
            lines.extend(
                [
                    f"Binary artifact: {metadata['mime_type']}, {metadata['size_bytes']} bytes, "
                    f"SHA-256 {metadata['sha256']}",
                    "",
                ]
            )
