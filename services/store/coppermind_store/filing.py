"""Retryable monthly filing. Originals never move; the note moves last.

A source's non-text artifacts (a PDF, an image) are filed as artifacts: the
bytes join the artifact archive under their own identity, a browsable copy
lands under the artifacts folder keyed on the source's ingest date, and the
note cites it. The old `_Attachments/` copy is retired; filing never writes
there again.
"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import sqlalchemy as sa

from coppermind import frontmatter as fm
from coppermind.atomicio import atomic_write_bytes, create_exclusive_bytes, sync_directory
from coppermind.db.models import Note, NoteSource, SearchDocument
from coppermind.db.session import transaction
from coppermind.embeds import rewrite_embeds
from coppermind.ids import is_valid_id
from coppermind.naming import titled_stem, unique_stem
from coppermind.schema import date_field_problem, note_date
from coppermind.store_protocol import (
    MoveNote,
    PatchFrontmatter,
    RenameNote,
    ReplaceNote,
    StoreError,
    ValidationFailed,
)
from coppermind_store.eligibility import FilingRoots, rendered_note_folder
from coppermind_store.fs import content_hash, existing_stems, resolve
from coppermind_store.maintenance import writer

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore


KEPT_HASHES = 16


def _keep_hash(plan: dict, digest: str) -> None:
    hashes = [h for h in plan.get("hashes", []) if h != digest]
    plan["hashes"] = [*hashes, digest][-KEPT_HASHES:]


def _save(path: Path, value: dict) -> None:
    atomic_write_bytes(
        path,
        json.dumps(value, sort_keys=True).encode(),
        mode=0o600 if path.parent.name == "filing" else 0o644,
    )


def filing_state(store, note_id: str) -> dict:
    path = store.control.store.state_dir / "filing" / f"{note_id}.json"
    try:
        plan = json.loads(path.read_bytes()) if path.exists() else {}
        return {"filing_error": plan.get("error"), "filing_attempts": plan.get("attempts", 0)}
    except (OSError, ValueError):
        return {"filing_error": "Filing receipt is unreadable", "filing_attempts": 0}


def remember_note(store, plan: dict, note) -> None:
    data = resolve(store.notes_root, note.path).read_bytes()
    if content_hash(data) != note.content_hash:
        raise ValueError("note changed before filing snapshot")
    snapshot = {
        "path": note.path,
        "hash": note.content_hash,
        "content": base64.b64encode(data).decode(),
    }
    copies = plan.setdefault("copies", [])
    if not copies or copies[-1] != snapshot:
        copies.append(snapshot)
    _keep_hash(plan, note.content_hash)
    _save(store.control.store.state_dir / "filing" / f"{note.id}.json", plan)


def vanished_removal(store, row):
    """A committed removal of a journaled origin proves a vanished move's removal."""
    from coppermind_store.note_history import Removal

    path = store.control.store.state_dir / "filing" / f"{row.id}.json"
    if not path.exists():
        return None, None
    reason = f"File vanished between filing paths; last known hash {row.content_hash}"
    try:
        plan = json.loads(path.read_bytes())
        copies = plan.get("copies", [])
        if not copies and plan.get("complete") is not False:
            return None, None
        current = next((c for c in reversed(copies) if c["hash"] == row.content_hash), None)
        if current is None:
            return None, reason + "; no matching filing copy is available"
        data = base64.b64decode(current["content"], validate=True)
        if content_hash(data) != row.content_hash:
            return None, reason + "; filing copy failed hash verification"
        for copy in copies:
            evidence = store.note_history.evidence(row.id, copy["path"], copy["hash"])
            if evidence is not None:
                return Removal(
                    note_id=row.id,
                    path=row.path,
                    content_hash=row.content_hash,
                    removal_commit=evidence.removal_commit,
                    blob=evidence.blob,
                ), None
        return None, reason + "; awaiting Git evidence of removal"
    except (OSError, ValueError, KeyError, TypeError):
        return None, reason + "; filing receipt is unreadable"


def _copy(path: Path, data: bytes) -> None:
    try:
        create_exclusive_bytes(path, data)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != data:
            raise ValueError("filing destination is occupied") from None


_FILED_MONTH = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


def filed_source_path(
    sources_folder: str, manifest: dict, source_id: str, month: str, stem: str | None = None
) -> str:
    """Where a source's filed page belongs: nested by year under the sources root.

    A page already filed keeps its month and its name, so a date correction never moves it
    or renames it; one filed flat as `{sources}/{yyyy-mm}/` is carried into its year folder.
    A page not yet filed takes `stem` for its name when one is given, and its id otherwise.
    """
    filed = manifest.get("filed_projection_path")
    if not filed:
        return f"{sources_folder}/{month[:4]}/{month}/{stem or source_id}.md"
    parent, _, name = filed.rpartition("/")
    folder, _, filed_month = parent.rpartition("/")
    if folder == sources_folder and _FILED_MONTH.fullmatch(filed_month):
        return f"{sources_folder}/{filed_month[:4]}/{filed_month}/{name}"
    return filed


def _is_transcript(revision: dict) -> bool:
    """Whether a source's current revision holds a transcript, as a recording's does."""
    return any(
        str(artifact.get("name", "")).startswith("transcript.")
        for artifact in revision.get("artifacts", [])
    )


def _page_stem(title: str, revision: dict) -> str:
    """What a filed source page is called: "<title> transcript", or "<title> source"."""
    # A note's title can carry the date its file is named with; the page name does not need it.
    title = re.sub(r"^\d{4}-\d{2}-\d{2}[ _-]+", "", title) or title
    return titled_stem(title, "transcript" if _is_transcript(revision) else "source")


def _taken_page_stems(folder: Path) -> set[str]:
    """Every page name in use in a sources folder, whatever stands at it."""
    try:
        return {entry.stem.casefold() for entry in folder.iterdir() if entry.suffix == ".md"}
    except (FileNotFoundError, NotADirectoryError):
        return set()


class SourceRevisionNeedsReview(ValueError):
    """The current archive revision was not part of this note's approval."""


def source_revisions(store: LocalStore, frontmatter: dict) -> dict[str, int | None]:
    """The current archive revision of each source a note cites (None when unreadable)."""
    from coppermind_store.sources import _manifest_document

    revisions: dict[str, int | None] = {}
    for source_id in sorted(set(frontmatter.get(store.control.schema().role("sources_key"), []))):
        try:
            manifest = _manifest_document(store.sources_root / source_id / "manifest.json")
            revisions[source_id] = manifest["current_revision"]
        except (OSError, ValueError, StoreError):
            # Missing evidence cannot become approved merely by appearing later.
            revisions[source_id] = None
    return revisions


def record_review(store: LocalStore, note_id: str, frontmatter: dict) -> None:
    """Persist the revisions approved by an explicit review, independently of filing."""
    reviewed = frontmatter.get(store.control.schema().role("reviewed_key")) is True
    _save(
        store.control.store.state_dir / "reviews" / f"{note_id}.json",
        {"reviewed": reviewed, "sources": source_revisions(store, frontmatter) if reviewed else {}},
    )


def carry_review(store: LocalStore, note_id: str, before: dict[str, int | None]) -> None:
    """Keep a note's approval across a rewrite of its own transcript that the owner asked for.

    Naming a voice from the note replaces speaker labels in the transcript, which makes a new
    source revision after the owner marked the note reviewed. The owner approved that note
    knowing the names would be applied, so each source the approval covered at `before` is
    approved at its new revision. A source whose approval was not at `before`, or that
    is not reviewed, is left alone.
    """
    path = store.control.store.state_dir / "reviews" / f"{note_id}.json"
    if not path.exists():
        return
    receipt = json.loads(path.read_bytes())
    if not receipt.get("reviewed"):
        return
    now = source_revisions(store, {store.control.schema().role("sources_key"): list(before)})
    moved = False
    for source_id, old in before.items():
        new = now.get(source_id)
        if (
            old is not None
            and new is not None
            and new != old
            and receipt.get("sources", {}).get(source_id) == old
        ):
            receipt["sources"][source_id] = new
            moved = True
    if moved:
        _save(path, receipt)


async def file_note(store: LocalStore, note_id: str) -> bool:
    from coppermind_store.artifacts import TRANSCRIPT_DATA, file_source_artifact
    from coppermind_store.sources import (
        _external_id_claim_path,
        _manifest_document,
        _manifest_revision,
        _manifest_time,
        _verify_revision_artifacts,
    )

    schema, settings = store.control.schema(), store.control.settings()
    note = await store.get_note(note_id)
    if note.frontmatter.get(schema.role("reviewed_key")) is not True:
        return False
    journal = store.control.store.state_dir / "filing" / f"{note_id}.json"
    plan = json.loads(journal.read_bytes()) if journal.exists() else None
    if store.context.config().lifecycle_enabled and (
        not plan
        or plan.get("content_hash") != note.content_hash
        or not await store.context.lifecycle.follow(note)
    ):
        await store.context.lifecycle.finalize(note)
        # Naming a voice from the note rewrites it; filing carries on with what is there now.
        note = await store.get_note(note_id)
    problem = date_field_problem(note.frontmatter, schema)
    if problem:
        raise ValueError(problem)
    day = note_date(note.frontmatter, schema)
    if day is None:
        raise ValueError("a note date is required in date")
    month = day.strftime("%Y-%m")
    dropbox = store.context.config().dropbox_folder
    roots = FilingRoots(settings, store.notes_root, dropbox)
    root, _ = roots.resolve(note.frontmatter.get(schema.role("type_key")))
    folder = rendered_note_folder(root, day)
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
            if plan and "sources" in plan
            else {key: value["current_revision"] for key, value in manifests.items()}
        )
        review = {"reviewed": True, "sources": approved}
        _save(review_path, review)
    # A completed receipt for this very folder means the note sits elsewhere
    # because someone moved it. A receipt for another folder is an upgrade or a
    # date edit, which is a re-file and not drift.
    hand_moved_from = (
        note.path
        if plan
        and plan.get("complete")
        and plan.get("folder") == folder
        and not note.path.startswith(folder + "/")
        else (plan or {}).get("hand_moved_from")
        if plan and plan.get("folder") == folder
        else None
    )
    same_filing = (
        plan
        and plan.get("complete")
        and plan.get("folder") == folder
        and note.path.startswith(folder + "/")
        and set(note.sources) == {item["id"] for item in plan["sources"]}
    )
    if not review.get("reviewed") or any(
        review["sources"].get(source_id) != manifest["current_revision"]
        for source_id, manifest in manifests.items()
    ):
        if not (plan and plan.get("complete")):
            # Never filed: the review no longer covers the new revision, so the note is an
            # ordinary note waiting for review again. An already filed note is not pulled back.
            await store.patch_frontmatter(
                note_id,
                PatchFrontmatter(set={schema.role("reviewed_key"): False}),
                if_match=note.content_hash,
            )
            return False
        if same_filing and review.get("reviewed"):
            # A pending source revision does not revoke the human note's review.
            # Its source association still authorizes only the earlier revision.
            await _publish(store, note)
        raise SourceRevisionNeedsReview("mark the note reviewed again to approve current sources")
    if (
        plan
        and plan.get("complete")
        and plan.get("folder") == folder
        and note.path.startswith(folder + "/")
        and set(note.sources) == {item["id"] for item in plan["sources"]}
        and all(
            item["revision"] == manifests[item["id"]]["current_revision"]
            and item["to"] == manifests[item["id"]]["projection_path"]
            and filed_source_path(
                settings.notes.sources_folder, manifests[item["id"]], item["id"], month
            )
            == manifests[item["id"]]["projection_path"]
            for item in plan["sources"]
        )
    ):
        await _publish(store, note)
        _settle(journal, plan)
        return False
    # Rebuild a stale plan from current durable manifests. Files already copied
    # remain preserved; the new plan can resume from their recorded paths.
    sources = []
    sources_folder = settings.notes.sources_folder
    chosen: dict[str, set[str]] = {}
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
        # Every path the note body may still link to stays outstanding until the
        # note rewrite succeeds, including a page an interrupted attempt moved.
        link_froms = {link_from, old_path}
        if previous:
            link_froms |= {previous["from"], previous["to"], *previous.get("link_froms", [])}
        stem = None
        if old_path.startswith(f"{sources_folder}/_Pending/") and not manifest.get(
            "filed_projection_path"
        ):
            # A page still waiting is named for what it is, not for its id. A page filed before
            # this (even one a flat month folder still holds) keeps the name it has.
            folder_now = f"{sources_folder}/{month[:4]}/{month}"
            if previous and previous["to"].rpartition("/")[0] == folder_now:
                stem = Path(previous["to"]).stem  # an interrupted attempt already named it
            else:
                taken = chosen.setdefault(
                    folder_now, _taken_page_stems(resolve(store.notes_root, folder_now))
                )
                revision = _manifest_revision(manifest, manifest["current_revision"])
                stem = unique_stem(_page_stem(note.title, revision), taken)
                taken.add(stem.casefold())
        sources.append(
            {
                "id": source_id,
                "from": old_path,
                "link_from": link_from,
                "link_froms": sorted(link_froms),
                "to": filed_source_path(sources_folder, manifest, source_id, month, stem),
                "revision": manifest["current_revision"],
            }
        )
    plan = {
        "note_id": note_id,
        "month": month,
        "folder": folder,
        "sources": sources,
        "complete": False,
        "attempts": (plan or {}).get("attempts", 0),
        "copies": (plan or {}).get("copies", []),
        "hashes": (plan or {}).get("hashes", []),
    }
    if hand_moved_from is not None:
        plan["hand_moved_from"] = hand_moved_from
    remember_note(store, plan, note)
    async with transaction(store.session_factory) as session:
        await session.execute(sa.update(Note).where(Note.id == note_id).values(filed=False))
        # A move changes the projection path without changing its archive revision.
        await session.execute(
            sa.delete(SearchDocument).where(
                SearchDocument.kind == "source", SearchDocument.ref_id.in_(note.sources)
            )
        )
    _save(journal, plan)
    filed_images: list[tuple[str, str, str]] = []
    filed_artifact_ids: list[str] = []
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
                # A recording's timings are already in the source page and the archive; a
                # file of them in Artifacts is clutter nobody opens.
                if artifact["name"] == TRANSCRIPT_DATA:
                    continue
                name = artifact["name"]
                if Path(name).name != name or "\\" in name:
                    raise ValueError("invalid artifact filename")
                held = resolve(store.sources_root / source_id / f"r{item['revision']:04d}", name)
                artifact_id, filed_path = await file_source_artifact(
                    store,
                    source_id=source_id,
                    source_name=name,
                    provider=str(manifest.get("provider", "")),
                    mime_type=mime,
                    data=held,
                    received_at=_manifest_time(revision.get("ingested_at"), "ingested_at"),
                )
                links.append(f"[[{filed_path}]]")
                filed_images.append((source_id, name, filed_path))
                if artifact_id not in filed_artifact_ids:
                    filed_artifact_ids.append(artifact_id)
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
        or refreshed_day.strftime("%Y-%m") != month
        or roots.resolve(note.frontmatter.get(schema.role("type_key")))[0] != root
    ):
        raise ValueError("note review or date changed during filing")
    if set(note.sources) != {item["id"] for item in plan["sources"]}:
        raise ValueError("note source associations changed during filing")
    body = note.body
    for item in plan["sources"]:
        for old_link in {item["link_from"], item["from"], *item.get("link_froms", [])}:
            # A link with its own text ([[page|00:03:12]]) or a place in the page moves too.
            moved = "[[" + item["to"].removesuffix(".md")
            body = re.sub(
                r"\[\[" + re.escape(old_link.removesuffix(".md")) + r"(?=[\]|#])",
                moved.replace("\\", "\\\\"),
                body,
            )
    body = rewrite_embeds(body, filed_images, attachments_folder=settings.notes.attachments_folder)
    before_filing_hash = note.content_hash
    frontmatter = dict(note.frontmatter)
    artifacts_key = schema.optional_role("artifacts_key")
    cited = frontmatter.get(artifacts_key)
    current_citations = (
        [value for value in cited if isinstance(value, str)] if isinstance(cited, list) else []
    )
    added = [value for value in filed_artifact_ids if value not in current_citations]
    if added:
        frontmatter[artifacts_key] = current_citations + added
    if body != note.body or frontmatter != note.frontmatter:
        note = await store.replace_note(
            note_id, ReplaceNote(frontmatter=frontmatter, body=body), note.content_hash
        )
    remember_note(store, plan, note)
    if not note.path.startswith(folder + "/"):
        plan["target_folder"] = folder
        _save(journal, plan)
        note = await _free_name(store, note, folder)
        note = await store.move_note(note_id, MoveNote(target_folder=folder), note.content_hash)
    if hand_moved_from is not None:
        store.control.record_reconciler_notice(
            note_id=note_id,
            title=note.title,
            from_path=hand_moved_from,
            path=note.path,
            folder=folder,
            reason=(
                f"Moved back to {folder}/ because reviewed notes are filed by "
                "type and note date; a hand move outside that folder is not kept."
            ),
            at=datetime.now(tz=UTC),
        )
        plan.pop("hand_moved_from", None)
    plan["complete"] = True
    plan["content_hash"] = note.content_hash
    plan.pop("error", None)
    _save(journal, plan)
    await _publish(store, note)
    await store.entities.follow(note, before_filing_hash)
    return True


def _title_for(stem: str, title: str) -> str:
    """The title to rename to so the file is called `stem`: a date prefix is not part of it."""
    parts = stem.split(" ", 1)
    if len(parts) == 2 and len(parts[0]) == 10 and parts[0][4] == "-" and parts[0][7] == "-":
        return parts[1]
    return stem


async def _free_name(store: LocalStore, note, folder: str):
    """Give a note whose name is taken in `folder` the next free " (2)", " (3)"... name.

    Only the file's name changes; the note's own heading and text stay as written. Nothing
    already in the folder is ever replaced.
    """
    target = resolve(store.notes_root, folder)
    for _ in range(10):
        stem = Path(note.path).stem
        in_folder = {name.casefold() for name in existing_stems(target)}
        if stem.casefold() not in in_folder:
            return note
        beside = existing_stems(resolve(store.notes_root, str(Path(note.path).parent)))
        free = unique_stem(stem, [*in_folder, *(name for name in beside if name != stem)])
        note = await store.rename_note(
            note.id,
            RenameNote(title=_title_for(free, note.title), preserve_body=True),
            note.content_hash,
        )
    raise ValidationFailed([f'The note could not be given a free name in "{folder}".'])


def _settle(journal: Path, plan: dict) -> None:
    """A later pass found the published note in place, so no filing is in flight."""
    changed = plan.pop("error", None) is not None
    copies = plan.pop("copies", None)
    for copy in copies or []:
        if isinstance(copy, dict) and isinstance(copy.get("hash"), str):
            _keep_hash(plan, copy["hash"])
    changed = copies is not None or changed
    if changed:
        _save(journal, plan)


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


@writer
async def file_reviewed(store: LocalStore) -> dict[str, int]:
    async with store._filing_lock:
        return await _file_reviewed(store)


def _tidy_legacy_month_folders(store: LocalStore) -> None:
    """Remove emptied flat `YYYY-MM/` folders; one that still holds anything is reported."""
    sources_folder = store.control.settings().notes.sources_folder.strip("/")
    for prefix in ("", sources_folder):
        root = resolve(store.notes_root, prefix) if prefix else store.notes_root
        try:
            folders = [
                entry
                for entry in root.iterdir()
                if _FILED_MONTH.fullmatch(entry.name) and entry.is_dir() and not entry.is_symlink()
            ]
        except OSError:
            continue
        for entry in folders:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            key = f"folder:{relative}"
            try:
                if any(entry.iterdir()):
                    if key not in store.control.read_reconciler_notices():
                        store.control.record_reconciler_notice(
                            note_id=key,
                            title=f"{relative}/ was kept",
                            from_path=relative,
                            path=relative,
                            folder=relative,
                            reason=(
                                f"{relative}/ is an old flat month folder, but it still holds "
                                "files, so it was left in place. Move or delete what is in it; "
                                "an empty one is removed on the next filing pass."
                            ),
                            at=datetime.now(tz=UTC),
                            kind="folder",
                        )
                else:
                    entry.rmdir()
                    sync_directory(root)
                    store.control.clear_reconciler_notice(key)
            except OSError:
                continue


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
    failed_ids: set[str] = set()
    started = time.monotonic()
    for note_id in ids:
        path = store.control.store.state_dir / "filing" / f"{note_id}.json"
        try:
            retrying = filing_state(store, note_id)["filing_error"] is not None
            changed = await file_note(store, note_id)
            counts["filed"] += int(changed)
            if changed or retrying:
                state = json.loads(path.read_bytes())
                state["attempts"] = state.get("attempts", 0) + 1
                _save(path, state)
            if changed:
                # Make the estimate useful during the first large pass, rather
                # than waiting until its entire queue has already drained.
                _record_rate(
                    store,
                    {
                        "seconds_per_note": (time.monotonic() - started) / counts["filed"],
                        "last_progress_at": datetime.now(UTC).isoformat(),
                        "stuck": False,
                    },
                )
        except Exception as exc:
            counts["filing_failed"] += 1
            failed_ids.add(note_id)
            if not isinstance(exc, SourceRevisionNeedsReview):
                async with transaction(store.session_factory) as session:
                    await session.execute(
                        sa.update(Note).where(Note.id == note_id).values(filed=False)
                    )
            path = store.control.store.state_dir / "filing" / f"{note_id}.json"
            try:
                state = json.loads(path.read_bytes()) if path.exists() else {"note_id": note_id}
                state["attempts"] = state.get("attempts", 0) + 1
                state["error"] = (
                    f"{type(exc).__name__}: {exc}"
                    if type(exc) is ValueError and str(exc)
                    else type(exc).__name__
                )
                _save(path, state)
            except (OSError, ValueError):
                from coppermind_store.reconciler import log

                log.warning("filing failure could not be recorded", note_id=note_id)
    if ids and not counts["filed"]:
        async with store.session_factory() as session:
            unfiled = set(
                (
                    await session.scalars(
                        sa.select(Note.id).where(Note.id.in_(ids), Note.filed.is_(False))
                    )
                ).all()
            )
        stuck = bool(unfiled - failed_ids)
    else:
        stuck = False
    _record_rate(store, {"stuck": stuck, "last_pass_at": datetime.now(UTC).isoformat()})
    _tidy_legacy_month_folders(store)
    return counts


def _record_rate(store: LocalStore, update: dict) -> None:
    """Keep the drain estimate, and whether the last pass left drainable work unfiled."""
    path = store.control.store.state_dir / "filing-rate.json"
    try:
        try:
            rate = json.loads(path.read_bytes())
        except (OSError, ValueError):
            rate = {}
        if all(rate.get(key) == value for key, value in update.items()):
            return
        _save(path, {**rate, **update})
    except OSError:
        from coppermind_store.reconciler import log

        log.warning("filing drain estimate could not be recorded")
