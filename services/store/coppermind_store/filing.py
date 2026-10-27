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
from coppermind.schema import date_field_problem, note_date, parse_note_time
from coppermind.store_protocol import MoveNote, ReplaceNote, StoreError
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


class SourceRevisionNeedsReview(ValueError):
    """The current archive revision was not part of this note's approval."""


def record_review(store: LocalStore, note_id: str, frontmatter: dict) -> None:
    """Persist the revisions approved by an explicit review, independently of filing."""
    from coppermind_store.sources import _manifest_document

    schema = store.control.schema()
    reviewed = frontmatter.get(schema.role("reviewed_key")) is True
    revisions = {}
    if reviewed:
        for source_id in sorted(set(frontmatter.get(schema.role("sources_key"), []))):
            try:
                manifest = _manifest_document(store.sources_root / source_id / "manifest.json")
                revisions[source_id] = manifest["current_revision"]
            except (OSError, ValueError, StoreError):
                # Missing evidence cannot become approved merely by appearing later.
                revisions[source_id] = None
    _save(
        store.control.store.state_dir / "reviews" / f"{note_id}.json",
        {"reviewed": reviewed, "sources": revisions},
    )


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
    if store.context.config().lifecycle_enabled:
        await store.context.lifecycle.finalize(note)
    problem = date_field_problem(note.frontmatter, schema)
    if problem:
        raise ValueError(problem)
    legacy = parse_note_time(note.frontmatter.get("time"))
    if (
        note.frontmatter.get(schema.role("type_key")) == "work"
        and legacy.present
        and not legacy.valid
    ):
        raise ValueError("enter a valid local meeting time")
    day = note_date(note.frontmatter, schema)
    if day is None:
        raise ValueError("a note date is required in date")
    folder = day.strftime("%Y-%m")
    journal = store.control.store.state_dir / "filing" / f"{note_id}.json"
    plan = json.loads(journal.read_bytes()) if journal.exists() else None
    manifests = {}
    for source_id in sorted(set(note.sources)):
        if not is_valid_id(source_id):
            raise ValueError("invalid source identity")
        manifests[source_id] = _manifest_document(store.sources_root / source_id / "manifest.json")
    review_path = store.control.store.state_dir / "reviews" / f"{note_id}.json"
    if review_path.exists():
        review = json.loads(review_path.read_bytes())
    else:
        # Existing receipts already identify the approved revision. Never upgrade
        # their approval from a newer manifest during restore or a date edit.
        approved = (
            {item["id"]: item["revision"] for item in plan.get("sources", [])}
            if plan
            else {key: value["current_revision"] for key, value in manifests.items()}
        )
        review = {"reviewed": True, "sources": approved}
        _save(review_path, review)
    same_filing = (
        plan
        and plan.get("complete")
        and plan.get("month") == folder
        and note.path.startswith(folder + "/")
        and set(note.sources) == {item["id"] for item in plan["sources"]}
    )
    if not review.get("reviewed") or any(
        review["sources"].get(source_id) != manifest["current_revision"]
        for source_id, manifest in manifests.items()
    ):
        if same_filing and review.get("reviewed"):
            # A pending source revision does not revoke the human note's review.
            # Its source association still authorizes only the earlier revision.
            await _publish(store, note)
        raise SourceRevisionNeedsReview("mark the note reviewed again to approve current sources")
    if (
        plan
        and plan.get("complete")
        and plan.get("month") == folder
        and note.path.startswith(folder + "/")
        and set(note.sources) == {item["id"] for item in plan["sources"]}
        and all(
            item["revision"] == manifests[item["id"]]["current_revision"]
            and item["to"] == manifests[item["id"]]["projection_path"]
            for item in plan["sources"]
        )
    ):
        await _publish(store, note)
        if plan.pop("error", None) is not None:
            _save(journal, plan)
        return False
    # Rebuild a stale plan from current durable manifests. Files already copied
    # remain preserved; the new plan can resume from their recorded paths.
    sources = []
    for source_id in sorted(set(note.sources)):
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
                "to": manifest.get("filed_projection_path")
                or f"{settings.notes.sources_folder}/{folder}/{source_id}.md",
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
                source_month = Path(item["to"]).parent.name
                attachment = (
                    f"{settings.notes.attachments_folder}/{source_month}/"
                    f"{source_id}-r{item['revision']:04d}-{name}"
                )
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
            manifest["filed_projection_path"] = item["to"]
            _save(manifest_path, manifest)
            if old != target and old.exists():
                if old.read_bytes() != original_projection:
                    raise ValueError("source projection changed during filing")
                old.unlink()
                sync_directory(old.parent)
    # Refresh after source work, preserving edits that arrived while sources were filed.
    note = await store.get_note(note_id)
    refreshed_day = note_date(note.frontmatter, schema)
    if (
        note.frontmatter.get(schema.role("reviewed_key")) is not True
        or refreshed_day is None
        or refreshed_day.strftime("%Y-%m") != folder
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
    review_path = store.control.store.state_dir / "reviews" / f"{note.id}.json"
    approved = json.loads(review_path.read_bytes())["sources"]
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

        for source_id in sorted(set(note.sources)):
            session.add(
                NoteSource(
                    note_id=note.id,
                    source_id=source_id,
                    approved_revision=approved.get(source_id),
                    created_at=datetime.now(tz=UTC),
                )
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
            if not isinstance(exc, SourceRevisionNeedsReview):
                async with transaction(store.session_factory) as session:
                    await session.execute(
                        sa.update(Note).where(Note.id == note_id).values(filed=False)
                    )
            path = store.control.store.state_dir / "filing" / f"{note_id}.json"
            try:
                state = json.loads(path.read_bytes()) if path.exists() else {"note_id": note_id}
                state["error"] = (
                    f"{type(exc).__name__}: {exc}"
                    if type(exc) is ValueError and str(exc)
                    else type(exc).__name__
                )
                _save(path, state)
            except (OSError, ValueError):
                from coppermind_store.reconciler import log

                log.warning("filing failure could not be recorded", note_id=note_id)
    return counts
