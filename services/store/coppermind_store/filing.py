"""Retryable monthly filing. Originals never move; the note moves last."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import sqlalchemy as sa

from coppermind import frontmatter as fm
from coppermind.atomicio import atomic_write_bytes, create_exclusive_bytes, sync_directory
from coppermind.db.models import Note, NoteSource, SearchDocument
from coppermind.db.session import transaction
from coppermind.ids import is_valid_id
from coppermind.store_protocol import MoveNote, ReplaceNote
from coppermind_store.fs import resolve

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore


def _save(path: Path, value: dict) -> None:
    atomic_write_bytes(path, json.dumps(value, sort_keys=True).encode())


def _copy(path: Path, data: bytes) -> None:
    try:
        create_exclusive_bytes(path, data)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != data:
            raise ValueError("filing destination is occupied") from None


async def file_note(store: LocalStore, note_id: str) -> bool:
    from coppermind_store.sources import (
        _external_id_claim_path,
        _manifest_document,
        _manifest_revision,
        _verify_revision_artifacts,
    )

    schema, settings = store.control.schema(), store.control.settings()
    note = await store.get_note(note_id)
    if note.frontmatter.get(schema.role("reviewed_key")) is not True:
        return False
    folder = str(note.frontmatter[schema.role("date_key")])[:7]
    # The schema has already validated the date; reject anything that is not a month.
    datetime.strptime(folder, "%Y-%m")
    journal = store.control.store.state_dir / "filing" / f"{note_id}.json"
    plan = json.loads(journal.read_bytes()) if journal.exists() else None
    if (
        plan
        and plan.get("complete")
        and plan.get("month") == folder
        and note.path.startswith(folder + "/")
        and set(note.sources) == {item["id"] for item in plan["sources"]}
    ):
        await _publish(store, note)
        return False
    # Rebuild a stale plan from current durable manifests. Files already copied
    # remain preserved; the new plan can resume from their recorded paths.
    sources = []
    for source_id in note.sources:
        if not is_valid_id(source_id):
            raise ValueError("invalid source identity")
        manifest = _manifest_document(store.sources_root / source_id / "manifest.json")
        previous = next(
            (item for item in (plan or {}).get("sources", []) if item["id"] == source_id), None
        )
        old_path = manifest["projection_path"]
        # Keep the original link target after a partial source move.
        link_from = previous.get("link_from", previous["from"]) if previous else old_path
        sources.append(
            {
                "id": source_id,
                "from": old_path,
                "link_from": link_from,
                "to": f"{settings.notes.sources_folder}/{folder}/{source_id}.md",
                "revision": manifest["current_revision"],
            }
        )
    plan = {"note_id": note_id, "month": folder, "sources": sources, "complete": False}
    async with transaction(store.session_factory) as session:
        await session.execute(sa.update(Note).where(Note.id == note_id).values(filed=False))
        # A move changes the projection path without changing its archive revision.
        await session.execute(
            sa.delete(SearchDocument).where(
                SearchDocument.kind == "source", SearchDocument.ref_id.in_(note.sources)
            )
        )
    _save(journal, plan)
    for item in plan["sources"]:
        source_id = item["id"]
        manifest_path = store.sources_root / source_id / "manifest.json"
        manifest = _manifest_document(manifest_path)
        claim = _external_id_claim_path(
            store.sources_root, manifest["provider"], manifest["external_source_id"]
        )
        async with store._source_lock_for(claim.name):
            manifest = _manifest_document(manifest_path)
            if manifest["current_revision"] != item["revision"]:
                raise ValueError("source changed during filing; pending review is preserved")
            revision = _manifest_revision(manifest, item["revision"])
            _verify_revision_artifacts(
                store.sources_root / source_id, item["revision"], revision["artifacts"]
            )
            old = resolve(store.notes_root, item["from"])
            target = resolve(store.notes_root, item["to"])
            current = target if not old.exists() else old
            original_projection = current.read_bytes()
            parsed, body = fm.parse(original_projection.decode("utf-8"))
            if (
                parsed.get("source_id") != source_id
                or parsed.get("source_revision") != item["revision"]
                or parsed.get("managed") is not True
            ):
                raise ValueError("source projection identity mismatch")
            parsed["reviewed"] = True
            links = []
            for artifact in revision["artifacts"]:
                mime = artifact["mime_type"]
                # Text already appears in the source page. Audio remains archive-only.
                if mime.startswith(("text/", "audio/")):
                    continue
                name = artifact["name"]
                if Path(name).name != name or "\\" in name:
                    raise ValueError("invalid artifact filename")
                attachment = f"{settings.notes.attachments_folder}/{folder}/{source_id}-{name}"
                data = resolve(
                    store.sources_root / source_id / f"r{item['revision']:04d}", name
                ).read_bytes()
                _copy(resolve(store.notes_root, attachment), data)
                links.append(f"[[{attachment}]]")
            if links:
                section = "\n\n## Filed attachments\n\n" + "\n".join(links) + "\n"
                if section not in body:
                    body += section
            data = fm.compose(parsed, body).encode()
            if old == target:
                # A prior completed file is already reviewed; never replace unexpected bytes.
                if target.read_bytes() != original_projection:
                    raise ValueError("existing monthly projection changed")
                if original_projection != data:
                    atomic_write_bytes(target, data)
            else:
                _copy(target, data)
            manifest["projection_path"] = item["to"]
            _save(manifest_path, manifest)
            if old != target and old.exists():
                if old.read_bytes() != original_projection:
                    raise ValueError("source projection changed during filing")
                old.unlink()
                sync_directory(old.parent)
    # Refresh after source work, preserving edits that arrived while sources were filed.
    note = await store.get_note(note_id)
    if (
        note.frontmatter.get(schema.role("reviewed_key")) is not True
        or str(note.frontmatter.get(schema.role("date_key"), ""))[:7] != folder
    ):
        raise ValueError("note review or date changed during filing")
    if set(note.sources) != {item["id"] for item in plan["sources"]}:
        raise ValueError("note source associations changed during filing")
    body = note.body
    for item in plan["sources"]:
        for old_link in {item["link_from"], item["from"]}:
            body = body.replace(
                f"[[{old_link.removesuffix('.md')}]]", f"[[{item['to'].removesuffix('.md')}]]"
            )
    if body != note.body:
        note = await store.replace_note(
            note_id, ReplaceNote(frontmatter=note.frontmatter, body=body), note.content_hash
        )
    if not note.path.startswith(folder + "/"):
        note = await store.move_note(note_id, MoveNote(target_folder=folder), note.content_hash)
    plan["complete"] = True
    plan.pop("error", None)
    _save(journal, plan)
    await _publish(store, note)
    return True


async def _publish(store, note):
    async with transaction(store.session_factory) as session:
        updated = await session.execute(
            sa.update(Note)
            .where(
                Note.id == note.id,
                Note.content_hash == note.content_hash,
                Note.reviewed.is_(True),
                Note.path == note.path,
            )
            .values(
                filed=True,
                source_ids=sorted(set(note.sources)),
                filed_source_ids=sorted(set(note.sources)),
            )
            .returning(Note.id)
        )
        if updated.scalar_one_or_none() is None:
            raise ValueError("note changed before publication")
        await session.execute(sa.delete(NoteSource).where(NoteSource.note_id == note.id))
        from datetime import UTC

        for source_id in note.sources:
            session.add(
                NoteSource(note_id=note.id, source_id=source_id, created_at=datetime.now(tz=UTC))
            )


async def file_reviewed(store: LocalStore) -> dict[str, int]:
    async with store._filing_lock:
        return await _file_reviewed(store)


async def _file_reviewed(store: LocalStore) -> dict[str, int]:
    """A failed item stays durable and visible; other items can finish."""
    async with store.session_factory() as session:
        ids = (
            await session.scalars(
                sa.select(Note.id)
                .where(Note.reviewed.is_(True), Note.state == "ok")
                .order_by(Note.id)
            )
        ).all()
    counts = {"filed": 0, "filing_failed": 0}
    for note_id in ids:
        try:
            counts["filed"] += int(await file_note(store, note_id))
        except Exception as exc:
            counts["filing_failed"] += 1
            async with transaction(store.session_factory) as session:
                await session.execute(sa.update(Note).where(Note.id == note_id).values(filed=False))
            path = store.control.store.state_dir / "filing" / f"{note_id}.json"
            try:
                state = json.loads(path.read_bytes()) if path.exists() else {"note_id": note_id}
                state["error"] = type(exc).__name__
                _save(path, state)
            except (OSError, ValueError):
                from coppermind_store.reconciler import log

                log.warning("filing failure could not be recorded", note_id=note_id)
    return counts
