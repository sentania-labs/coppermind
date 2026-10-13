"""Restore archive and source-linked note mirrors without altering originals."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import sqlalchemy as sa

from coppermind import frontmatter as fm
from coppermind.db.models import Note, NoteSource
from coppermind.db.session import transaction
from coppermind.ids import is_valid_id
from coppermind_store.fs import content_hash, resolve

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore


async def recover_sources(store: LocalStore) -> dict[str, int]:
    from coppermind_store.notes import _jsonable, _mirror_columns, _title_of
    from coppermind_store.sources import (
        _ensure_source_mirror,
        _manifest_document,
        _verify_revision_artifacts,
    )

    schema = store.control.schema()
    settings = store.control.settings()
    manifests = {}
    # Verify all originals before writing any mirror rows. A damaged backup is an error.
    for path in sorted(store.sources_root.glob("*/manifest.json")):
        if not is_valid_id(path.parent.name) or path.is_symlink() or path.parent.is_symlink():
            raise ValueError("invalid source archive path")
        manifest = _manifest_document(path)
        if manifest.get("source_id") != path.parent.name:
            raise ValueError("source manifest identity mismatch")
        for revision in manifest["revisions"]:
            _verify_revision_artifacts(path.parent, revision["revision"], revision["artifacts"])
        manifests[path.parent.name] = manifest
    candidates = {}
    duplicates = set()
    for path in sorted(store.notes_root.rglob("*.md")):
        relative = path.relative_to(store.notes_root).as_posix()
        if any(
            part.startswith(".") or part == settings.notes.trash_folder
            for part in Path(relative).parts
        ):
            continue
        try:
            data = resolve(store.notes_root, relative).read_bytes()
            frontmatter, body = fm.parse(data.decode())
        except (OSError, ValueError, UnicodeDecodeError, fm.FrontmatterError):
            continue
        note_id = frontmatter.get(schema.role("id_key"))
        if frontmatter.get("managed") or not is_valid_id(note_id):
            continue
        if note_id in candidates:
            duplicates.add(note_id)
        candidates[note_id] = (relative, data, frontmatter, body, path.stat().st_mtime)
    now = datetime.now(tz=UTC)
    restored = 0
    async with transaction(store.session_factory) as session:
        for manifest in manifests.values():
            await _ensure_source_mirror(session, manifest)
        for note_id, (relative, data, frontmatter, body, mtime) in candidates.items():
            if note_id in duplicates:
                continue
            cited = frontmatter.get(schema.role("sources_key"), [])
            if not isinstance(cited, list) or any(
                not isinstance(source, str) or source not in manifests for source in cited
            ):
                continue
            # Reconciliation owns existing rows and validation failures; recovery restores identity.
            if await session.get(Note, note_id) is None:
                errors = schema.validate_frontmatter(frontmatter)
                session.add(
                    Note(
                        id=note_id,
                        path=relative,
                        title=_title_of(body, Path(relative)),
                        content_hash=content_hash(data),
                        size_bytes=len(data),
                        mtime=datetime.fromtimestamp(mtime, tz=UTC),
                        frontmatter=_jsonable(frontmatter),
                        **_mirror_columns(frontmatter, schema),
                        state="unparsed" if errors else "ok",
                        state_reason="; ".join(errors) or None,
                        first_seen_at=datetime.fromtimestamp(mtime, tz=UTC),
                        updated_at=now,
                    )
                )
                await session.flush()
                restored += 1
            await session.execute(sa.delete(NoteSource).where(NoteSource.note_id == note_id))
            for source_id in cited:
                session.add(NoteSource(note_id=note_id, source_id=source_id, created_at=now))
    return {"recovered_sources": len(manifests), "recovered_notes": restored}
