"""Delete means gone: drop what the earlier release kept about deleted items, in one pass.

Before this revision a deleted note left records behind for a keep time and then waited
for a manual clean-up: its mirror row (`state` `deleted`), the recording sources only it
opened, a recovery receipt under `state/note-recovery`, the bytes Admin itself took away
under `state/admin-removed`, and the archive of a removed artifact. Issue 143 makes a
deletion answer once, so this pass removes all of it. Notes that are only missing, which
Git has not recorded as removed, are left alone: their saved filing copy still restores them.

The vault's Git history is the safety net from here on and is never written by a migration.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-23
"""

from __future__ import annotations

import json
import re
from contextlib import suppress
from pathlib import Path

import sqlalchemy as sa
from alembic import op

from coppermind.atomicio import atomic_write_bytes

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_IDENTITY = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
# What the release that wrote these archives marked as the owner's own removal.
_OWNER_REMOVALS = {"removed in Admin", "removed through the API", "deleted in Obsidian"}


def _remove_tree(root: Path) -> None:
    """Delete a folder of files this release wrote, without following a symlink."""
    if not root.is_dir() or root.is_symlink():
        return
    for path in sorted(root.rglob("*"), reverse=True):
        with suppress(OSError):
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            else:
                path.unlink()
    with suppress(OSError):
        root.rmdir()


def _on_disk(notes_dir: Path) -> tuple[set[str], dict[str, list[Path]], bool]:
    """Every identity a note file in the vault mentions, and the pages made for sources.

    Wider than the notes' own identities and citations on purpose: anything a note names
    stays. A page Coppermind wrote for a source names that source and does not count; it is
    listed by source instead, so it can go with the source. The last value is False when the
    folder is missing or a file could not be read, so the scan is not a complete picture.
    """
    from coppermind import frontmatter as fm

    found: set[str] = set()
    pages: dict[str, list[Path]] = {}
    complete = notes_dir.is_dir()
    if not complete:
        return found, pages, False
    for path in notes_dir.rglob("*.md"):
        relative = path.relative_to(notes_dir)
        if any(part in {".git", ".obsidian"} for part in relative.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            complete = False
            continue
        with suppress(fm.FrontmatterError, ValueError):
            fields, _ = fm.parse(text)
            if fields.get("managed") is True:
                if isinstance(fields.get("source_id"), str):
                    pages.setdefault(fields["source_id"], []).append(path)
                continue
        found.update(_IDENTITY.findall(text))
    return found, pages, complete


def _orphaned_sources(
    bind: sa.engine.Connection, gone: set[str], mentioned: set[str], sources_dir: Path
) -> list[tuple[str, Path, list[Path]]]:
    """The sources only the deleted notes opened: (id, bundle, files outside it to remove)."""
    from coppermind_store.sources import _external_id_claim_path

    links = bind.execute(
        sa.text("SELECT source_id, note_id FROM note_sources ORDER BY created_at, note_id")
    ).all()
    firsts: dict[str, str] = {}
    other_notes: set[str] = set()
    for source_id, note_id in links:
        firsts.setdefault(source_id, note_id)
        if note_id not in gone:
            other_notes.add(source_id)
    cited = {
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT DISTINCT unnest(source_ids) FROM notes "
                "WHERE state <> 'deleted' AND source_ids IS NOT NULL"
            )
        )
    }
    candidates = {source_id for source_id, note_id in links if note_id in gone}
    found = []
    for row in bind.execute(
        sa.text("SELECT id, provider, external_source_id FROM sources ORDER BY id")
    ).all():
        source_id, provider, external = row
        if source_id not in candidates or source_id in other_notes | cited | mentioned:
            continue
        bundle = sources_dir / source_id
        if bundle.is_symlink():
            continue
        # A bundle already gone, from a run that failed after removing it, still has its row.
        has_files = bundle.is_dir() and any(not p.is_dir() for p in bundle.rglob("*"))
        opening = firsts.get(source_id)
        if has_files:
            try:
                manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue  # A damaged copy is not proved to belong to a deleted note.
            opening = manifest.get("note_id") if isinstance(manifest, dict) else None
        if opening not in gone:
            continue
        outside = [_external_id_claim_path(sources_dir, provider, external)]
        found.append((source_id, bundle, outside))
    return found


def upgrade() -> None:
    from coppermind.settings import Wiring

    wiring = Wiring()
    bind = op.get_bind()

    # A note whose file a scan can still see is not deleted, whatever its row says: it goes
    # back to an ordinary note and the next scan reads it.
    # With the volume missing, or a vault file unreadable, nothing on disk can be proved
    # unreferenced, so only the rows are dropped and every source and archive stays.
    on_disk, pages, mounted = _on_disk(wiring.notes_dir)
    present: list[str] = []
    deleted: set[str] = set()
    for (note_id,) in bind.execute(sa.text("SELECT id FROM notes WHERE state = 'deleted'")):
        if note_id in on_disk:
            present.append(note_id)
        else:
            deleted.add(note_id)
    for note_id in present:
        bind.execute(
            sa.text("UPDATE notes SET state = 'ok', state_reason = NULL WHERE id = :id"),
            {"id": note_id},
        )
    if deleted:
        orphans = _orphaned_sources(bind, deleted, on_disk, wiring.sources_dir) if mounted else []
        for source_id, bundle, outside in orphans:
            for path in (*pages.get(source_id, []), *outside):
                with suppress(OSError):
                    path.unlink(missing_ok=True)
            _remove_tree(bundle)
            # Rows reach the search index and the notes through their own cascades.
            bind.execute(sa.text("DELETE FROM sources WHERE id = :id"), {"id": source_id})
            bind.execute(
                sa.text("DELETE FROM search_documents WHERE kind = 'source' AND ref_id = :id"),
                {"id": source_id},
            )
        for note_id in sorted(deleted):
            bind.execute(sa.text("DELETE FROM notes WHERE id = :id"), {"id": note_id})
            bind.execute(
                sa.text("DELETE FROM search_documents WHERE kind = 'note' AND ref_id = :id"),
                {"id": note_id},
            )
            with suppress(OSError):
                (wiring.state_dir / "filing" / f"{note_id}.json").unlink(missing_ok=True)
        if wiring.notes_dir.is_dir():
            # What the people and companies pages and the voice store learned from these notes
            # is scrubbed by the store when it next starts, which holds the code that knows how.
            pending = wiring.state_dir / "removed-notes-pending.json"
            pending.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(
                pending, json.dumps({"note_ids": sorted(deleted)}).encode(), mode=0o600
            )

    # An artifact the owner removed kept its archive for the keep time. Its row went
    # when it was removed, so an archive still marked removed has nothing left to keep.
    live = {row[0] for row in bind.execute(sa.text("SELECT id FROM artifacts"))}
    for manifest_file in sorted(wiring.artifacts_dir.glob("*/manifest.json")):
        archive = manifest_file.parent
        if archive.name in live or archive.is_symlink():
            continue
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            isinstance(manifest, dict)
            and manifest.get("artifact_id") == archive.name
            and manifest.get("removed_at")
            and manifest.get("removed_reason") in _OWNER_REMOVALS
        ):
            _remove_tree(archive)
            with suppress(OSError):
                (wiring.state_dir / "artifacts" / f"{archive.name}.json").unlink(missing_ok=True)

    state = wiring.state_dir
    # The count, the problem list and the clean-up receipts belonged to the clean-up screen.
    for name in ("cleanup_count.json", "purge-problems.json"):
        with suppress(OSError):
            (state / name).unlink(missing_ok=True)
    _remove_tree(state / "purges")
    # Receipts and Admin's own removed copies were the old second safety net.
    for folder in ("note-recovery", "admin-removed"):
        _remove_tree(state / folder)

    # The keep time is gone from the settings file.
    from coppermind.settings import drop_retired_settings
    from coppermind.statefiles import StateStore

    with suppress(OSError, ValueError):
        drop_retired_settings(StateStore(state))


def downgrade() -> None:
    # The rows and copies were removed by design; a scan rebuilds what still exists.
    pass
