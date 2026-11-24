"""Delete means gone: a note's removal drops what the store kept about it, in one call.

When a note is removed, its own records go with it at once: the mirror row, its search
entry, the saved filing information, what the people and companies pages and the voice
store learned from it, and the sources only it opened. No recovery copy is kept and
nothing waits for a keep time or a clean-up. The vault's Git history is the safety net
and is never written here.

The work is ordered so a failure can be repeated safely. Files and saved records go
first and the rows last, so a note whose removal stopped part way still has its row, is
still found absent, and is removed again on the next pass.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from coppermind import frontmatter as fm
from coppermind.atomicio import sync_directory
from coppermind.db.models import EntityRecord, Note, NoteSource, SearchDocument, Source
from coppermind.db.session import transaction
from coppermind.ids import is_valid_id
from coppermind.logging import get_logger
from coppermind.store_protocol import NotesFilesystemUnavailable
from coppermind_store import indexer
from coppermind_store.damaged_sources import bundle_state, note_damaged, note_repaired
from coppermind_store.fs import resolve as filesystem_path
from coppermind_store.sources import _external_id_claim_path

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore

log = get_logger("coppermind-store")

_IDENTITY = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")

# Notes removed per call. The people and companies pages are scrubbed once per call.
BATCH = 500


def resolve(root: Path, relative: str) -> Path:
    """A path under `root` that is safe to remove: no dot parts, no `.git`, no symlinks."""
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in {".", "..", ".git"} for p in parts):
        raise ValueError("unsafe removal path")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("removal refuses symlinks")
    return filesystem_path(root, relative)


def disk_references(store: LocalStore) -> tuple[set[str], set[str], dict[str, list[str]]]:
    """Which notes the vault holds, which sources they cite, and the pages written for sources.

    Fails closed on a note it cannot read, and includes notes the mirror does not hold. A note
    it cannot parse is not an error: whatever sources it names are kept.
    """
    ids: set[str] = set()
    cited: set[str] = set()
    pages: dict[str, list[str]] = {}
    schema = store.control.schema()
    if not store.notes_root.is_dir() or not store.sources_root.is_dir():
        raise NotesFilesystemUnavailable("Notes or sources volume unavailable")
    for path in sorted(store.notes_root.rglob("*.md")):
        relative = path.relative_to(store.notes_root)
        if any(part in {".git", ".obsidian"} for part in relative.parts):
            continue
        data = resolve(store.notes_root, relative.as_posix()).read_text(errors="replace")
        try:
            fields, _ = fm.parse(data)
        except (fm.FrontmatterError, ValueError):
            # A note that cannot be parsed may still cite a source, so anything it names stays.
            cited.update(_IDENTITY.findall(data))
            continue
        # Only verified generated pages are exempt from being a live citation.
        if (
            fields.get("managed") is True
            and is_valid_id(fields.get("source_id"))
            and not is_valid_id(fields.get(schema.role("id_key")))
        ):
            pages.setdefault(fields["source_id"], []).append(relative.as_posix())
            continue
        identity = fields.get(schema.role("id_key"))
        if isinstance(identity, str):
            ids.add(identity)
        sources = fields.get(schema.role("sources_key"), [])
        if not isinstance(sources, list) or any(not isinstance(s, str) for s in sources):
            cited.update(_IDENTITY.findall(data))
            continue
        cited.update(sources)
    return ids, cited, pages


def _bundle_paths(root: Path, bundle: Path) -> tuple[list[Path], list[Path]]:
    """Every file under a bundle, and its folders deepest first, each checked to stay inside."""
    files: list[Path] = []
    folders: list[Path] = []
    for child in sorted(bundle.rglob("*")):
        resolve(root, child.relative_to(root).as_posix())
        (folders if child.is_dir() else files).append(child)
    folders.sort(key=lambda p: len(p.parts), reverse=True)
    return files, folders


def _orphans(
    store: LocalStore,
    removed: set[str],
    candidates: list[Source],
    firsts: dict[str, str],
    cited: set[str],
    disk_ids: set[str],
    pages: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """The sources only the removed notes opened, with the files each one holds."""
    found = []
    for source in candidates:
        if source.id in cited:
            continue
        if not is_valid_id(source.id):
            raise ValueError("invalid source identity")
        bundle = resolve(store.sources_root, source.id)
        state, manifest = bundle_state(bundle)
        if state == "damaged":
            note_damaged(store, source.id, source.provider, source.external_source_id)
            continue
        note_repaired(store, source.id, "manifest")
        if state == "ok":
            opening = manifest.get("note_id")
            # A legacy source with no opening identity cannot be proved to belong to this note.
            if not is_valid_id(opening) or opening not in removed or opening in disk_ids:
                continue
            if manifest.get("source_id") != source.id:
                raise ValueError("source manifest identity mismatch")
        else:
            opening = firsts.get(source.id)
            if opening is None or opening not in removed or opening in disk_ids:
                continue
        files, folders = _bundle_paths(store.sources_root, bundle)
        folders.append(bundle)
        # The manifest and the claim go last: they are what a retry reads to know whose
        # bundle this is, so a removal that stops part way can still be finished.
        files.sort(key=lambda path: path.name == "manifest.json")
        claim = _external_id_claim_path(
            store.sources_root, source.provider, source.external_source_id
        )
        if claim.exists():
            files.append(claim)
        pages_here = []
        recorded = manifest.get("projection_path") if state == "ok" else None
        for projection in sorted(
            set(pages.get(source.id, [])) | ({recorded} if recorded else set())
        ):
            path = resolve(store.notes_root, projection)
            if path.exists():
                fields, _ = fm.parse(path.read_text())
                if fields.get("managed") is not True or fields.get("source_id") != source.id:
                    raise ValueError("source projection identity mismatch")
                pages_here.append(path)
        found.append(
            {
                "id": source.id,
                "claim": claim.name,
                "files": files,
                "folders": folders,
                "pages": pages_here,
            }
        )
    return found


def _unlink(paths: Iterable[Path]) -> None:
    for path in paths:
        with suppress(FileNotFoundError):
            path.unlink()
            sync_directory(path.parent)


def _remove_folders(folders: Iterable[Path]) -> None:
    for folder in folders:
        with suppress(OSError):  # Never recursively erase a file nobody listed.
            folder.rmdir()
            sync_directory(folder.parent)


async def remove_notes(store: LocalStore, note_ids: Iterable[str]) -> set[str]:
    """Remove every record of these notes now. Returns the ids whose rows are gone.

    The caller has found each note absent from the vault and Git history is left alone.
    Callers already hold the maintenance gate as an ordinary writer.
    """
    wanted = sorted({i for i in note_ids if is_valid_id(i)})
    removed: set[str] = set()
    for start in range(0, len(wanted), BATCH):
        removed |= await _remove_batch(store, set(wanted[start : start + BATCH]))
    return removed


async def _remove_batch(store: LocalStore, wanted: set[str]) -> set[str]:
    async with store.session_factory() as session:
        rows = list((await session.scalars(sa.select(Note).where(Note.id.in_(wanted)))).all())
        held = {row.id for row in rows}
        mentioned = {s for row in rows for s in row.source_ids}
        links = list(
            (
                await session.scalars(
                    sa.select(NoteSource).where(
                        sa.or_(
                            NoteSource.note_id.in_(wanted),
                            NoteSource.source_id.in_(mentioned or {""}),
                        )
                    )
                )
            ).all()
        )
        opened = {link.source_id for link in links if link.note_id in wanted}
        candidate_ids = opened | mentioned
        firsts: dict[str, str] = {}
        for link in sorted(links, key=lambda link: (link.created_at, link.note_id)):
            firsts.setdefault(link.source_id, link.note_id)
        sources = (
            list(
                (await session.scalars(sa.select(Source).where(Source.id.in_(candidate_ids)))).all()
            )
            if candidate_ids
            else []
        )
        # What else still cites a candidate: another note's link or its own list of sources.
        shared = set()
        if sources:
            ids = [s.id for s in sources]
            shared.update(
                (
                    await session.scalars(
                        sa.select(NoteSource.source_id).where(
                            NoteSource.source_id.in_(ids), NoteSource.note_id.not_in(wanted)
                        )
                    )
                ).all()
            )
            shared.update(
                (
                    await session.scalars(
                        sa.select(sa.func.unnest(Note.source_ids)).where(
                            Note.id.not_in(wanted), Note.source_ids.overlap(ids)
                        )
                    )
                ).all()
            )
    candidates = [s for s in sources if s.id not in shared]
    orphans: list[dict[str, Any]] = []
    if candidates:
        disk_ids, cited, pages = await asyncio.to_thread(disk_references, store)
        orphans = await asyncio.to_thread(
            _orphans, store, wanted, candidates, firsts, cited, disk_ids, pages
        )
    gone_people = await asyncio.to_thread(store.entities.orphaned_by, wanted)

    # Files and saved records first, so a failure leaves the rows to try again from. Each
    # source goes under the lock ingest, merge and filing take to cite one, and is looked at
    # once more there: a note that cited it since the check above keeps it.
    removed_sources = 0
    for orphan in orphans:
        async with store._source_lock_for(orphan["claim"]):
            if await _cited_now(store, orphan["id"], wanted):
                continue
            await asyncio.to_thread(_unlink, [*orphan["pages"], *orphan["files"]])
            await asyncio.to_thread(_remove_folders, orphan["folders"])
            async with transaction(store.session_factory) as session:
                await indexer.drop_source(session, orphan["id"], datetime.now(UTC))
                await session.execute(sa.delete(Source).where(Source.id == orphan["id"]))
            removed_sources += 1
    for person in gone_people:
        await asyncio.to_thread(_unlink, [resolve(store.notes_root, person.path)])
    journals = store.control.store.state_dir / "filing"
    await asyncio.to_thread(_unlink, [journals / f"{note_id}.json" for note_id in wanted])
    person_ids = {person.id for person in gone_people}
    # The voice store and the context records are written under the context lock, and the
    # scrub reads and rewrites them, so it holds that lock and the ledger's for its length.
    async with store.context.lock, store.entities.lock:
        store.entities.purge(wanted, person_ids, secrets.token_hex(16), suppress=False)
        await store.entities.mirror(store.entities.load())

    now = datetime.now(UTC)
    async with transaction(store.session_factory) as session:
        for note_id in wanted:
            await indexer.drop_note(session, note_id, now)
        await session.execute(sa.delete(Note).where(Note.id.in_(wanted)))
        if person_ids:
            await session.execute(sa.delete(EntityRecord).where(EntityRecord.id.in_(person_ids)))
            await session.execute(
                sa.delete(SearchDocument).where(
                    SearchDocument.kind == "entity", SearchDocument.ref_id.in_(person_ids)
                )
            )
    if held:
        log.info("notes removed", count=len(held), sources=removed_sources)
    return held


async def _cited_now(store: LocalStore, source_id: str, removed: set[str]) -> bool:
    """Whether a note that is staying cites this source, read fresh from the mirror."""
    async with store.session_factory() as session:
        linked = await session.scalar(
            sa.select(sa.func.count())
            .select_from(NoteSource)
            .where(NoteSource.source_id == source_id, NoteSource.note_id.not_in(removed))
        )
        listed = await session.scalar(
            sa.select(sa.func.count())
            .select_from(Note)
            .where(Note.id.not_in(removed), Note.source_ids.contains([source_id]))
        )
    return bool(linked or listed)


async def finish_upgrade_removals(store: LocalStore) -> int:
    """Scrub what the upgrade that removed the old deleted-note rows could not reach.

    The upgrade drops those rows and their files, then lists the notes here. What the
    people and companies pages and the voice store learned from them goes now, and the
    list with it. A failure leaves the list for the next start.
    """
    path = store.control.store.state_dir / "removed-notes-pending.json"
    try:
        listed = json.loads(path.read_bytes()).get("note_ids", [])
    except (OSError, ValueError, AttributeError):
        return 0
    ids = [i for i in listed if isinstance(i, str)]
    async with store.maintenance.shared():
        await remove_notes(store, ids)
    await asyncio.to_thread(_unlink, [path])
    return len(ids)
