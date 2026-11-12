"""Notes as Admin shows them: where a note is, why one is stuck, and the fix.

Admin never opens the notes filesystem. Everything here is the store reading
its own mirror and files, and every change is the store making it, so the store
stays the one writer. Nothing is changed unless a person pressed the button for
it: a stuck note is reported with both files untouched until then.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from coppermind import frontmatter as fm
from coppermind.atomicio import atomic_write_bytes, create_exclusive_bytes, sync_directory
from coppermind.db.models import Note
from coppermind.naming import sanitize_folder, unique_stem
from coppermind.store_protocol import (
    NotFound,
    PathCollision,
    ProblemInfo,
    RenameNote,
    RestoreNote,
    StoreError,
    ValidationFailed,
)
from coppermind_store.fs import content_hash, existing_stems, resolve

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore

# Problems that are about one note and need its owner, by the kind the store records.
STUCK_KINDS = {
    "filing": "notfiled",
    "collision": "copy",
    "missing": "missing",
    "unparsed": "unreadable",
    "unreadable_note": "unreadable",
}
CONFIDENT = 0.8


def _now() -> str:
    return datetime.now(UTC).isoformat()


def words(text: str) -> int:
    return len(text.split())


def copy_path(reason: str) -> str:
    """The path a recorded collision is about. The reason always starts with it."""
    for marker in (" is a symbolic link", " is a copy of this note", " carries "):
        if marker in reason:
            return reason.split(marker, 1)[0]
    return ""


class AdminNotes:
    def __init__(self, store: LocalStore) -> None:
        self.store = store

    # -- files Admin itself took away, kept so they can be brought back at once -------------

    @property
    def removed_dir(self) -> Path:
        return self.store.control.store.state_dir / "admin-removed"

    def removed(self) -> list[dict[str, Any]]:
        found = []
        for path in sorted(self.removed_dir.glob("*.json")):
            try:
                found.append(json.loads(path.read_bytes()))
            except (OSError, ValueError):
                continue
        return found

    def _keep_removed(self, *, note_id: str, title: str, path: str, data: bytes, kind: str) -> str:
        key = hashlib.sha256(f"{path}\0{_now()}".encode()).hexdigest()[:32]
        atomic_write_bytes(
            self.removed_dir / f"{key}.json",
            json.dumps(
                {
                    "key": key,
                    "note_id": note_id,
                    "title": title,
                    "path": path,
                    "kind": kind,
                    "content": base64.b64encode(data).decode(),
                    "content_hash": content_hash(data),
                    "removed_at": _now(),
                }
            ).encode(),
            mode=0o600,
        )
        return key

    def _drop_removed(self, key: str) -> None:
        if len(key) == 32 and all(c in "0123456789abcdef" for c in key):
            (self.removed_dir / f"{key}.json").unlink(missing_ok=True)

    async def _take_away(self, relative: str, *, note_id: str, title: str, kind: str) -> str:
        """Remove one file from the notes filesystem, keeping its bytes for recovery."""
        path = resolve(self.store.notes_root, relative)
        data = await asyncio.to_thread(path.read_bytes)
        key = self._keep_removed(note_id=note_id, title=title, path=relative, data=data, kind=kind)
        await asyncio.to_thread(path.unlink)
        await asyncio.to_thread(sync_directory, path.parent)
        return key

    # -- lists ---------------------------------------------------------------------------

    async def _rows(self, *conditions: Any, limit: int = 500) -> list[Note]:
        async with self.store.session_factory() as session:
            return list(
                (
                    await session.scalars(
                        sa.select(Note)
                        .where(*conditions)
                        .order_by(Note.first_seen_at.desc())
                        .limit(limit)
                    )
                ).all()
            )

    async def _titles(self, ids: set[str]) -> dict[str, Note]:
        if not ids:
            return {}
        async with self.store.session_factory() as session:
            rows = (await session.scalars(sa.select(Note).where(Note.id.in_(sorted(ids))))).all()
        return {row.id: row for row in rows}

    async def stuck(self, problems: list[ProblemInfo]) -> list[dict[str, Any]]:
        """Every note Coppermind could not deal with by itself, one entry per note."""
        found: dict[str, dict[str, Any]] = {}
        for problem in problems:
            kind = STUCK_KINDS.get(problem.kind)
            if kind is None or not problem.note_id:
                continue
            entry = found.setdefault(
                problem.note_id,
                {"id": problem.note_id, "kind": kind, "reason": problem.reason, "copies": []},
            )
            if kind == "copy":
                entry["kind"] = "copy"
                path = copy_path(problem.reason)
                if path:
                    entry["copies"].append(path)
        rows = await self._titles(set(found))
        out = []
        for note_id, entry in found.items():
            row = rows.get(note_id)
            entry["title"] = (row.title if row else None) or Path(row.path if row else note_id).stem
            entry["path"] = row.path if row else ""
            entry["when"] = (row.mtime or row.updated_at).isoformat() if row else ""
            entry["name_taken"] = entry["kind"] == "notfiled" and self._filing_error(note_id) == (
                "PathCollision"
            )
            out.append(entry)
        # Notes the store refused to take on are files, not notes yet: named by their path.
        for problem in problems:
            if problem.kind == "adoption":
                out.append(
                    {
                        "id": "",
                        "kind": "cannot_add",
                        "title": Path(problem.reference).stem,
                        "path": problem.reference,
                        "reason": problem.reason,
                        "action": problem.action or "",
                        "copies": [],
                        "when": "",
                        "name_taken": False,
                    }
                )
        return out

    def _filing_error(self, note_id: str) -> str:
        journal = self.store.control.store.state_dir / "filing" / f"{note_id}.json"
        try:
            return str(json.loads(journal.read_bytes()).get("error") or "")
        except (OSError, ValueError):
            return ""

    def _open_suggestions(self) -> dict[str, int]:
        """How many suggestions on each waiting note still wait for the owner's answer."""
        latest: dict[str, Any] = {}
        for job in sorted(self.store.context.jobs(), key=lambda item: item.created_at):
            if job.purpose != "dossier":
                latest[job.note_id] = job
        counts = {}
        for note_id, job in latest.items():
            if job.state != "suggested" or job.result is None:
                continue
            decided = job.decided | {
                d["skipped"] for d in job.decisions if isinstance(d.get("skipped"), int)
            }
            count = sum(
                1
                for index, suggestion in enumerate(job.result.suggestions)
                if index not in decided and suggestion.kind != "filename"
            )
            if count:
                counts[note_id] = count
        return counts

    async def unsure(self) -> list[dict[str, Any]]:
        counts = self._open_suggestions()
        rows = await self._titles(set(counts))
        return [
            {
                "id": note_id,
                "title": rows[note_id].title or Path(rows[note_id].path).stem,
                "count": count,
            }
            for note_id, count in counts.items()
            if note_id in rows and rows[note_id].state == "ok" and not rows[note_id].reviewed
        ]

    async def waiting(self) -> list[dict[str, Any]]:
        asked = {job.note_id for job in self.store.context.jobs() if job.purpose != "dossier"}
        unsure = self._open_suggestions()
        rows = await self._rows(Note.state == "ok", Note.reviewed.is_(False))
        return [
            {
                "id": row.id,
                "title": row.title or Path(row.path).stem,
                "path": row.path,
                "arrived": row.first_seen_at.isoformat(),
                "unsure": unsure.get(row.id, 0),
                "no_suggestions": row.id not in asked,
            }
            for row in rows
        ]

    async def deleted(self) -> list[dict[str, Any]]:
        """Deleted notes that can still be brought back, the same list wherever it is shown."""
        rows = await self._rows(Note.state == "deleted")
        from coppermind_store import note_history

        receipts = await asyncio.to_thread(note_history.receipts, self.store)
        out = [
            {
                "id": row.id,
                "key": "",
                "title": row.title or Path(row.path).stem,
                "path": row.path,
                "deleted_at": max(row.updated_at, receipts[row.id].recorded_at).isoformat()
                if row.id in receipts
                else row.updated_at.isoformat(),
                "size": row.size_bytes,
            }
            for row in rows
            if row.state_reason != "Merged into another note"
        ]
        known = {row.id for row in rows}
        for item in self.removed():
            # Once history has recorded the removal, the note is listed from there.
            if item["kind"] == "note" and item["note_id"] in known:
                self._drop_removed(item["key"])
                continue
            out.append(
                {
                    "id": item["note_id"] if item["kind"] == "note" else "",
                    "key": item["key"],
                    "title": item["title"],
                    "path": item["path"],
                    "deleted_at": item["removed_at"],
                    "size": len(base64.b64decode(item["content"])),
                }
            )
        out.sort(key=lambda item: item["deleted_at"], reverse=True)
        return out

    async def left_alone(self) -> list[dict[str, str]]:
        exclusions = self.store.control.read_reconciler_exclusions()
        return [
            {"title": Path(path).stem, "path": path, "why": reason}
            for path, reason in sorted(exclusions.items())
        ]

    async def find(self, query: str) -> list[dict[str, Any]]:
        """Every note whose name contains the words typed, filed and deleted ones included."""
        text = query.strip()
        if not text:
            return []
        pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = await self._rows(
            sa.or_(Note.title.ilike(pattern, escape="\\"), Note.path.ilike(pattern, escape="\\")),
            limit=50,
        )
        return [self._summary(row) for row in rows]

    def _summary(self, row: Note) -> dict[str, Any]:
        state = (
            "deleted"
            if row.state == "deleted"
            else "missing"
            if row.state == "missing"
            else "filed"
            if row.reviewed and row.filed
            else "reviewed"
            if row.reviewed
            else "waiting"
        )
        return {
            "id": row.id,
            "title": row.title or Path(row.path).stem,
            "path": row.path,
            "state": state,
            "arrived": row.first_seen_at.isoformat(),
            "changed": (row.mtime or row.updated_at).isoformat(),
            "deleted_at": row.updated_at.isoformat() if row.state == "deleted" else "",
        }

    async def counts(self) -> dict[str, int]:
        async with self.store.session_factory() as session:
            waiting = await session.scalar(
                sa.select(sa.func.count()).where(Note.state == "ok", Note.reviewed.is_(False))
            )
            total = await session.scalar(sa.select(sa.func.count()).where(Note.state == "ok"))
        return {"waiting": int(waiting or 0), "total": int(total or 0)}

    # -- one note ------------------------------------------------------------------------

    async def _file_facts(self, relative: str) -> dict[str, Any] | None:
        try:
            path = resolve(self.store.notes_root, relative)
            data = await asyncio.to_thread(path.read_bytes)
            stat = await asyncio.to_thread(path.stat)
        except (OSError, ValueError):
            return None
        try:
            _, body = fm.parse(data.decode("utf-8"))
        except (fm.FrontmatterError, UnicodeDecodeError):
            body = data.decode("utf-8", errors="replace")
        return {
            "path": relative,
            "name": Path(relative).stem,
            "folder": str(Path(relative).parent),
            "edited": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            "words": words(body),
            "body": body,
        }

    async def detail(self, payload: dict[str, Any]) -> dict[str, Any]:
        note_id = str(payload["id"])
        async with self.store.session_factory() as session:
            row = await session.get(Note, note_id)
        if row is None:
            raise NotFound(note_id)
        result = self._summary(row)
        result["reviewed"] = row.reviewed
        result["history"] = await self._history(row)
        result["unsure"] = self._open_suggestions().get(note_id, 0)
        from coppermind_store.reconciler import note_collisions

        copies = [copy_path(reason) for reason in await note_collisions(self.store, note_id)]
        copies = [path for path in copies if path]
        error = self._filing_error(note_id)
        if copies and row.state == "ok":
            original = await self._file_facts(row.path)
            others = [facts for path in copies if (facts := await self._file_facts(path))]
            if original and others:
                result["stuck"] = "copy"
                result["original"] = _without_body(original)
                result["copy"] = _without_body(others[0])
                result["difference"] = _difference(original["body"], others[0]["body"])
                result["original_filed"] = bool(row.reviewed and row.filed)
        elif error and row.state == "ok" and row.reviewed:
            result["stuck"] = "notfiled"
            result["filing_error"] = error
            journal = self.store.control.store.state_dir / "filing" / f"{note_id}.json"
            plan = json.loads(journal.read_bytes())
            folder = str(plan.get("folder") or "")
            mine = await self._file_facts(row.path)
            if error == "PathCollision" and folder and mine:
                taken = await self._file_facts(f"{folder}/{Path(row.path).name}")
                if taken:
                    result["name_taken"] = True
                    result["mine"] = _without_body(mine)
                    result["taken"] = _without_body(taken)
                    result["folder"] = folder
                    result["both_as"] = unique_stem(
                        Path(row.path).stem,
                        existing_stems(resolve(self.store.notes_root, folder)),
                    )
            if not result.get("name_taken"):
                from coppermind_store.sources import _filing_reason

                result["reason"] = _filing_reason(error)
        elif row.state == "missing":
            result["stuck"] = "missing"
            try:
                await self.store.preview_note_restore(note_id)
                result["can_restore"] = True
            except StoreError:
                result["can_restore"] = False
        elif row.state == "unparsed":
            result["stuck"] = "unreadable"
            result["reason"] = row.state_reason or ""
        return result

    async def _history(self, row: Note) -> list[dict[str, str]]:
        """What happened to one note, as sentences, oldest first."""
        events: list[tuple[str, str]] = []
        recording = next(
            (
                job
                for job in self.store.context.recordings.jobs()
                if job.note_id == row.id and job.succeeded_at
            ),
            None,
        )
        events.append(
            (
                row.first_seen_at.isoformat(),
                f'Arrived from the recording "{recording.title}".' if recording else "Arrived.",
            )
        )
        for job in sorted(self.store.context.jobs(), key=lambda item: item.created_at):
            if job.note_id != row.id or job.purpose == "dossier":
                continue
            filled = [d for d in job.decisions if d.get("action") in {"approved", "applied"}]
            if job.succeeded_at:
                events.append(
                    (
                        job.succeeded_at,
                        f"Suggestions filled in {len(filled)} blank"
                        f"{'' if len(filled) == 1 else 's'}."
                        if filled
                        else "Suggestions were written for you to check.",
                    )
                )
            elif job.state == "failed" and job.error:
                events.append((job.created_at, "Suggestions could not be written: " + job.error))
        review = self.store.control.store.state_dir / "reviews" / f"{row.id}.json"
        if row.reviewed and review.exists():
            stamp = datetime.fromtimestamp(review.stat().st_mtime, tz=UTC).isoformat()
            events.append((stamp, "You marked it reviewed."))
        journal = self.store.control.store.state_dir / "filing" / f"{row.id}.json"
        if journal.exists():
            stamp = datetime.fromtimestamp(journal.stat().st_mtime, tz=UTC).isoformat()
            with contextlib.suppress(OSError, ValueError):
                plan = json.loads(journal.read_bytes())
                if plan.get("error") == "PathCollision":
                    events.append(
                        (stamp, "Could not be filed: a file with that name is already there.")
                    )
                elif plan.get("error"):
                    events.append((stamp, "Could not be filed."))
                elif plan.get("complete"):
                    events.append((stamp, f"Filed in {plan.get('folder', 'its folder')}."))
        if row.state == "deleted":
            events.append((row.updated_at.isoformat(), "Deleted."))
        events.sort(key=lambda item: item[0])
        return [{"at": at, "text": text} for at, text in events]

    # -- fixes ---------------------------------------------------------------------------

    async def _file_now(self, note_id: str) -> dict[str, Any]:
        """Try filing one note straight away and say what became of it."""
        from coppermind_store.filing import file_note

        journal = self.store.control.store.state_dir / "filing" / f"{note_id}.json"
        try:
            async with self.store._filing_lock:
                await file_note(self.store, note_id)
        except Exception as exc:  # noqa: BLE001 - the reason is recorded exactly as filing does
            state: dict[str, Any] = (
                json.loads(journal.read_bytes()) if journal.exists() else {"note_id": note_id}
            )
            state["attempts"] = int(state.get("attempts", 0)) + 1
            state["error"] = (
                f"{type(exc).__name__}: {exc}"
                if type(exc) is ValueError and str(exc)
                else type(exc).__name__
            )
            atomic_write_bytes(journal, json.dumps(state).encode(), mode=0o600)
            return {"filed": False, "error": state["error"]}
        note = await self.store.get_note(note_id)
        return {"filed": True, "path": note.path, "name": Path(note.path).stem}

    async def check_again(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._file_now(str(payload["id"]))

    async def keep_both(self, payload: dict[str, Any]) -> dict[str, Any]:
        """File the note beside the file that has its name, under the next free name."""
        note_id = str(payload["id"])
        detail = await self.detail({"id": note_id})
        if not detail.get("name_taken"):
            return await self._file_now(note_id)
        note = await self.store.get_note(note_id)
        relative, path = await self.store._locate(note_id)
        target = path.with_name(detail["both_as"] + path.suffix)
        if target.exists():
            raise PathCollision(str(target.relative_to(self.store.notes_root)))
        # Only the file's name changes: the note's own heading and text are left as written.
        await self.store.rename_note(
            note_id,
            RenameNote(title=_title_for(detail["both_as"], note.title), preserve_body=True),
            note.content_hash,
        )
        relative, path = await self.store._locate(note_id)
        if path.stem != detail["both_as"]:
            raise ValidationFailed([f"The note could not be given the name {detail['both_as']}."])
        return await self._file_now(note_id)

    async def replace(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Move the file that has the name to Deleted, then file this note in its place."""
        note_id = str(payload["id"])
        detail = await self.detail({"id": note_id})
        if not detail.get("name_taken"):
            return await self._file_now(note_id)
        taken = detail["taken"]["path"]
        other = await self._rows(Note.path == taken, Note.state == "ok", limit=1)
        await self._take_away(
            taken,
            note_id=other[0].id if other else "",
            title=detail["taken"]["name"],
            kind="note" if other else "copy",
        )
        if other:
            await self._mark_missing(other[0].id)
        result = await self._file_now(note_id)
        result["replaced"] = detail["taken"]["name"]
        return result

    async def _mark_missing(self, note_id: str) -> None:
        """Say in the mirror, at once, that a file Admin removed is gone."""
        from coppermind_store.reconciler import MISSING_REASON

        async with self.store.session_factory() as session, session.begin():
            await session.execute(
                sa.update(Note)
                .where(Note.id == note_id)
                .values(state="missing", state_reason=MISSING_REASON, updated_at=datetime.now(UTC))
            )

    async def resolve_copy(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Settle a note that has a copy: keep both, keep the original, or keep the copy."""
        note_id, choice = str(payload["id"]), str(payload["choice"])
        detail = await self.detail({"id": note_id})
        if detail.get("stuck") != "copy":
            raise ValidationFailed(["This note no longer has a copy."])
        original, copy = detail["original"], detail["copy"]
        schema = self.store.control.schema()
        copy_file = resolve(self.store.notes_root, copy["path"])
        if choice == "both":
            # The copy gives up the identity it shares, so the next scan takes it on as a
            # note of its own that has not been reviewed.
            data = await asyncio.to_thread(copy_file.read_bytes)
            text = fm.patch(
                data.decode("utf-8"),
                {schema.role("reviewed_key"): False},
                unset=[schema.role("id_key")],
            )
            await asyncio.to_thread(atomic_write_bytes, copy_file, text.encode("utf-8"))
        elif choice == "original":
            await self._take_away(copy["path"], note_id="", title=copy["name"], kind="copy")
        elif choice == "copy":
            data = await asyncio.to_thread(copy_file.read_bytes)
            await self._take_away(
                original["path"],
                note_id="",
                title=original["name"] + " (the original)",
                kind="copy",
            )
            review = sanitize_folder(self.store.control.settings().notes.review_folder)
            text = fm.patch(data.decode("utf-8"), {schema.role("reviewed_key"): False})
            target = resolve(self.store.notes_root, f"{review}/{original['name']}.md")
            if target != copy_file and target.exists():
                raise PathCollision(f"{review}/{original['name']}.md")
            await asyncio.to_thread(create_or_replace, copy_file, target, text.encode("utf-8"))
        else:
            raise ValidationFailed(["Choose what to do with the copy."])
        await self._rescan()
        return {"choice": choice, "original": original["name"], "copy": copy["name"]}

    async def _rescan(self) -> None:
        from coppermind_store.reconciler import reconcile_once

        with contextlib.suppress(StoreError):
            await reconcile_once(self.store, full=True, quiet_period_s=0)

    async def restore(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Bring back a deleted note, from history or from the copy Admin kept."""
        key = str(payload.get("key") or "")
        if key:
            item = next((i for i in self.removed() if i["key"] == key), None)
            if item is None:
                raise ValidationFailed(["That deleted file is no longer kept."])
            target = resolve(self.store.notes_root, item["path"])
            try:
                await asyncio.to_thread(
                    create_exclusive_bytes, target, base64.b64decode(item["content"])
                )
            except FileExistsError as exc:
                raise PathCollision(item["path"]) from exc
            self._drop_removed(key)
            await self._rescan()
            return {"title": item["title"], "path": item["path"]}
        note_id = str(payload["id"])
        preview = await self.store.preview_note_restore(note_id)
        result = await self.store.restore_note(
            note_id, RestoreNote(token=preview.token, confirmation=f"Restore {note_id}")
        )
        return {"title": Path(result.path).stem, "path": result.path}

    async def delete_for_good(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Delete one deleted note for good now, without waiting for its keep time."""
        key = str(payload.get("key") or "")
        if key:
            item = next((i for i in self.removed() if i["key"] == key), None)
            if item is None:
                raise ValidationFailed(["That deleted file is no longer kept."])
            self._drop_removed(key)
            return {"title": item["title"]}
        from coppermind.purge import ConfirmPurge
        from coppermind_store import purge

        note_id = str(payload["id"])
        async with self.store.session_factory() as session:
            row = await session.get(Note, note_id)
        if row is None or row.state != "deleted":
            raise ValidationFailed(["Only a deleted note can be deleted for good."])
        title = row.title or Path(row.path).stem
        preview = await purge.preview(self.store, only=note_id)
        if not any(c.kind == "note" and c.id == note_id for c in preview.candidates):
            raise ValidationFailed(
                [f'"{title}" cannot be deleted for good yet. Try again in a minute.']
            )
        await purge.confirm(
            self.store, ConfirmPurge(token=preview.token, confirmation="Purge listed items")
        )
        return {"title": title}

    async def remove_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Delete a note: its file leaves the notes filesystem and waits under Deleted."""
        note_id = str(payload["id"])
        async with self.store.session_factory() as session:
            row = await session.get(Note, note_id)
        if row is None or row.state != "ok":
            raise NotFound(note_id)
        title = row.title or Path(row.path).stem
        await self._take_away(row.path, note_id=note_id, title=title, kind="note")
        await self._mark_missing(note_id)
        return {"title": title}


def create_or_replace(source: Path, target: Path, data: bytes) -> None:
    """Write the note under its new name, then take the old name away."""
    if source == target:
        atomic_write_bytes(target, data)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    create_exclusive_bytes(target, data)
    source.unlink()
    sync_directory(source.parent)


def _without_body(facts: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in facts.items() if key != "body"}


def _title_for(stem: str, title: str) -> str:
    """The title to rename to so the file is called `stem`: the date prefix is not part of it."""
    parts = stem.split(" ", 1)
    if len(parts) == 2 and len(parts[0]) == 10 and parts[0][4] == "-" and parts[0][7] == "-":
        return parts[1]
    return stem


def _difference(original: str, copy: str) -> dict[str, Any]:
    """What differs between two versions of a note, as the lines only one of them has."""
    first, second = original.splitlines(), copy.splitlines()
    only_copy = [line for line in second if line.strip() and line not in first]
    only_original = [line for line in first if line.strip() and line not in second]
    return {
        "only_copy": only_copy[:5],
        "only_original": only_original[:5],
        "more": max(0, len(only_copy) - 5) + max(0, len(only_original) - 5),
        "same": not only_copy and not only_original,
    }
