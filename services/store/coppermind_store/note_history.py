"""Read Git evidence in batches; retain recovery copies outside the metadata mirror.

Git is only read here. No checkout, hooks, filters, remote access or ref writes
are involved. Immutable recovery receipts survive both mirror reconstruction
and Git housekeeping. A receipt is evidence, never authority to remove a file.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import sqlalchemy as sa
from pydantic import BaseModel

from coppermind import frontmatter as fm
from coppermind.atomicio import (
    atomic_write_bytes,
    create_exclusive_bytes,
    stage_bytes,
    sync_directory,
)
from coppermind.db.models import Note
from coppermind.db.session import transaction
from coppermind.ids import is_valid_id
from coppermind.store_protocol import (
    HistoryStatus,
    NoteRecovery,
    NotesFilesystemUnavailable,
    NotFound,
    PathCollision,
    RestoreNote,
    ValidationFailed,
    VersionConflict,
)
from coppermind_store.fs import content_hash, resolve

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore


class HistoryUnavailable(Exception):
    """No trustworthy Git evidence is available on this pass."""


class Receipt(BaseModel):
    schema_version: int = 1
    note_id: str
    path: str
    content_hash: str
    removal_commit: str
    blob: str
    content: str
    recorded_at: datetime

    def data(self) -> bytes:
        data = base64.b64decode(self.content, validate=True)
        if content_hash(data) != self.content_hash:
            raise ValueError("recovery copy hash mismatch")
        return data

    def token(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class GitHistory:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.head: str | None = None
        self.key: str | None = None
        self.removed: dict[str, list[tuple[str, str, str, bytes]]] = {}
        self.status = HistoryStatus(available=False, reason="Git evidence not checked yet")

    def git(self, *args: str, data: bytes | None = None) -> bytes:
        # Explicit discovery boundary: an unmounted vault must not discover a
        # parent repository. Refuse worktree pointers and symlinked Git dirs.
        if not (self.root / ".git").is_dir() or (self.root / ".git").is_symlink():
            raise HistoryUnavailable("Git repository unavailable on the notes volume")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_NO_REPLACE_OBJECTS="1",
        )
        try:
            result = subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(self.root / ".git"),
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "diff.external=",
                    *args,
                ],
                input=data,
                capture_output=True,
                env=env,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HistoryUnavailable(
                "Git executable unavailable or history query timed out"
            ) from exc
        if result.returncode:
            raise HistoryUnavailable("Git history could not be read; check the Git helper")
        return result.stdout

    def blobs(self, names: list[str]) -> dict[str, bytes]:
        if not names:
            return {}
        raw = self.git("cat-file", "--batch", data=("\n".join(names) + "\n").encode())
        found = {}
        offset = 0
        for name in names:
            end = raw.index(b"\n", offset)
            header = raw[offset:end].split()
            if len(header) != 3 or header[1] != b"blob":
                raise HistoryUnavailable("Git recovery object unavailable")
            size = int(header[2])
            offset = end + 1
            found[name] = raw[offset : offset + size]
            offset += size + 1
        return found

    def refresh(self, key: str, enabled: bool) -> None:
        try:
            if not enabled:
                raise HistoryUnavailable("Git history is disabled; absent notes remain missing")
            head = self.git("rev-parse", "--verify", "HEAD").decode().strip()
            if head != self.head or key != self.key:
                self._load(head, key)
                self.head, self.key = head, key
            self.status = HistoryStatus(
                available=True,
                head=head,
                reason="Git history readable; new removals await committed snapshots",
            )
        except (HistoryUnavailable, ValueError, UnicodeError) as exc:
            self.removed = {}
            self.head = None
            self.status = HistoryStatus(available=False, reason=str(exc))

    def _load(self, head: str, key: str) -> None:
        # One history walk and two bulk object reads per new HEAD, independent
        # of how many notes disappeared. NUL framing preserves unusual paths.
        fields = self.git(
            "log",
            "--first-parent",
            "--raw",
            "--no-abbrev",
            "--format=%H",
            "--no-renames",
            "-z",
            "--diff-filter=D",
            head,
        ).split(b"\0")
        commit = ""
        removals = []
        it = iter(fields)
        for field in it:
            field = field.lstrip(b"\n")
            if field.startswith(b":"):
                mode, _, blob, _, _ = field.split()
                path = next(it).decode("utf-8", "surrogateescape")
                if mode in {b":100644", b":100755"} and path.endswith(".md"):
                    removals.append((commit, blob.decode(), path))
            elif re.fullmatch(rb"[0-9a-f]{40,64}", field):
                commit = field.decode()
        deleted_blobs = self.blobs(sorted({blob for _, blob, _ in removals}))
        entries = self.git("ls-tree", "-r", "-z", head).split(b"\0")
        live_blobs = []
        for entry in entries:
            if entry:
                meta, tree_path = entry.split(b"\t", 1)
                mode, kind, tree_blob = meta.split()
                if (
                    kind == b"blob"
                    and mode in {b"100644", b"100755"}
                    and tree_path.endswith(b".md")
                ):
                    live_blobs.append(tree_blob.decode())
        live_ids = {
            self.identity(data, key) for data in self.blobs(sorted(set(live_blobs))).values()
        }
        removed: dict[str, list[tuple[str, str, str, bytes]]] = {}
        for commit, blob_name, path in removals:
            data = deleted_blobs[blob_name]
            note_id = self.identity(data, key)
            if note_id and note_id not in live_ids:
                removed.setdefault(note_id, []).append((commit, blob_name, path, data))
        self.removed = removed

    @staticmethod
    def identity(data: bytes, key: str) -> str | None:
        try:
            frontmatter, _ = fm.parse(data.decode())
            value = frontmatter.get(key)
            return value if is_valid_id(value) else None
        except (UnicodeError, fm.FrontmatterError):
            return None

    def evidence(self, note_id: str, path: str, digest: str) -> Receipt | None:
        candidates = self.removed.get(note_id, [])
        # Never fall back to an older deletion generation with matching bytes.
        latest = candidates[0][0] if candidates else None
        for commit, blob, old_path, data in candidates:
            if commit != latest:
                break
            if old_path == path and content_hash(data) == digest:
                return Receipt(
                    note_id=note_id,
                    path=path,
                    content_hash=digest,
                    removal_commit=commit,
                    blob=blob,
                    content=base64.b64encode(data).decode(),
                    recorded_at=datetime.now(UTC),
                )
        return None


def directory(store: LocalStore) -> Path:
    return store.control.store.state_dir / "note-recovery"


def save(store: LocalStore, receipt: Receipt) -> None:
    path = directory(store) / f"{receipt.note_id}-{receipt.removal_commit}.json"
    try:
        publish_receipt(path, receipt.model_dump_json().encode())
    except FileExistsError:
        previous = Receipt.model_validate_json(path.read_bytes())
        if previous.data() != receipt.data() or previous.path != receipt.path:
            raise NotesFilesystemUnavailable(
                "recovery receipt conflicts with existing evidence"
            ) from None


def receipts(store: LocalStore) -> dict[str, Receipt]:
    found: dict[str, Receipt] = {}
    try:
        for path in sorted(directory(store).glob("*.json")):
            receipt = Receipt.model_validate_json(path.read_bytes())
            if not is_valid_id(receipt.note_id):
                raise ValueError("invalid recovery identity")
            resolve(store.notes_root, receipt.path)
            receipt.data()
            old = found.get(receipt.note_id)
            if old is None or receipt.recorded_at > old.recorded_at:
                found[receipt.note_id] = receipt
    except (OSError, ValueError) as exc:
        raise NotesFilesystemUnavailable("note recovery evidence is unreadable") from exc
    return found


def publish_receipt(path: Path, data: bytes) -> None:
    """Publish only a complete fsynced file, without replacing an existing receipt."""
    staged = stage_bytes(path, data, mode=0o600)
    try:
        os.link(staged, path)
        sync_directory(path.parent)
    finally:
        staged.unlink(missing_ok=True)


def remember_return(store: LocalStore, note: Note, values: dict) -> None:
    """Remember later observations so an old deletion cannot win after DB loss."""
    if not any(directory(store).glob(f"{note.id}-*.json")):
        return
    fields = ("path", "content_hash", "frontmatter", "size_bytes", "title")
    value = {field: values.get(field, getattr(note, field)) for field in fields}
    value["recorded_at"] = datetime.now(UTC).isoformat()
    atomic_write_bytes(
        directory(store) / "observed" / f"{note.id}.json", json.dumps(value).encode(), mode=0o600
    )


async def recover_rows(store: LocalStore) -> None:
    """Seed absent identities before scanning, including when their files return."""
    from coppermind_store.notes import _jsonable, _mirror_columns, _title_of

    saved = await asyncio.to_thread(receipts, store)
    if not saved:
        return
    schema = store.control.schema()
    async with transaction(store.session_factory) as session:
        known = set((await session.scalars(sa.select(Note.id))).all())
        unknown = set(saved) - known
        if unknown:
            await asyncio.to_thread(
                store.note_history.refresh,
                schema.role("id_key"),
                store.control.settings().git.enabled,
            )
        for note_id in unknown:
            receipt = saved[note_id]
            observation = directory(store) / "observed" / f"{note_id}.json"
            later = json.loads(observation.read_bytes()) if observation.exists() else None
            # The latest removal in Git may have happened while the store was
            # offline. Do not resurrect an older receipt in preference to it.
            candidates = store.note_history.removed.get(note_id, [])
            state = "deleted"
            if candidates and candidates[0][0] != receipt.removal_commit:
                commit, blob, path, data = candidates[0]
                receipt = Receipt(
                    note_id=note_id,
                    path=path,
                    content_hash=content_hash(data),
                    removal_commit=commit,
                    blob=blob,
                    content=base64.b64encode(data).decode(),
                    recorded_at=datetime.now(UTC),
                )
                state = "missing"
                later = None
            data = receipt.data()
            frontmatter, body = fm.parse(data.decode())
            path, digest, size = receipt.path, receipt.content_hash, len(data)
            title = _title_of(body, Path(path))
            if later and datetime.fromisoformat(later["recorded_at"]) > receipt.recorded_at:
                path, digest, size = later["path"], later["content_hash"], later["size_bytes"]
                frontmatter, title = later["frontmatter"], later["title"]
                state = "missing"
            session.add(
                Note(
                    id=note_id,
                    path=path,
                    title=title,
                    content_hash=digest,
                    size_bytes=size,
                    frontmatter=_jsonable(frontmatter),
                    **_mirror_columns(frontmatter, schema),
                    state=state,
                    state_reason="recovered identity; awaiting scan",
                    first_seen_at=receipt.recorded_at,
                    updated_at=receipt.recorded_at,
                )
            )


async def preview(store: LocalStore, note_id: str) -> NoteRecovery:
    async with store.session_factory() as session:
        note = await session.get(Note, note_id)
        if note is None:
            raise NotFound(note_id)
        if note.state != "deleted":
            raise ValidationFailed(["Only a deleted note can be restored; reconcile and retry"])
    receipt = (await asyncio.to_thread(receipts, store)).get(note_id)
    if receipt is None or receipt.content_hash != note.content_hash or receipt.path != note.path:
        raise NotesFilesystemUnavailable("matching recovery evidence unavailable")
    return NoteRecovery(
        note_id=note_id,
        path=receipt.path,
        content=receipt.data().decode(),
        content_hash=receipt.content_hash,
        removal_commit=receipt.removal_commit,
        token=receipt.token(),
        recorded_at=receipt.recorded_at,
    )


async def restore(store: LocalStore, note_id: str, request: RestoreNote) -> NoteRecovery:
    from coppermind_store.reconciler import reconcile_once

    # Serialize scans with restore. The final exclusive creation is what also
    # protects against an external sync writer that does not share our locks.
    async with store._reconcile_lock:
        counts = await reconcile_once(store, full=True, quiet_period_s=30, locked=True)
        if counts["deferred"] or counts["duplicates"]:
            raise ValidationFailed(
                ["Files are settling or identities conflict; retry after reconciliation"]
            )
        async with store._lock_for(note_id):
            result = await preview(store, note_id)
            if result.token != request.token:
                raise VersionConflict(result.token)
            expected = f"Restore {note_id}"
            if request.confirmation != expected:
                raise ValidationFailed([f"Confirmation must be: {expected}"])
            target = resolve(store.notes_root, result.path)
            # Preserve exact authorization before a file becomes visible.
            audit = directory(store) / "restores" / f"{note_id}-{request.token}.json"
            authorization = request.model_dump_json().encode()
            try:
                publish_receipt(audit, authorization)
            except FileExistsError:
                if audit.read_bytes() != authorization:
                    raise NotesFilesystemUnavailable(
                        "restore authorization record is invalid"
                    ) from None
            try:
                create_exclusive_bytes(target, result.content.encode())
            except FileExistsError as exc:
                raise PathCollision(result.path) from exc
            except OSError as exc:
                raise NotesFilesystemUnavailable("restored file could not be written") from exc
        await reconcile_once(store, full=True, locked=True)
        return result


def helper_status(store: LocalStore, status: HistoryStatus) -> HistoryStatus:
    """Separate readable history from a healthy snapshot producer."""
    status = status.model_copy()
    path = store.control.store.state_dir / "git" / "status.json"
    try:
        value = json.loads(path.read_bytes())
        age = datetime.now(UTC).timestamp() - path.stat().st_mtime
        status.last_commit_at = (
            datetime.fromisoformat(value["last_commit_at"]) if value.get("last_commit_at") else None
        )
        status.dirty_files = int(value.get("dirty_files", 0))
        status.helper_status = (
            "disabled"
            if not value.get("enabled")
            else "stale"
            if age > 600
            else "error (inspect Git helper logs)"
            if value.get("last_error")
            else "running"
        )
    except (OSError, ValueError, TypeError):
        status.helper_status = "status unavailable"
    return status
