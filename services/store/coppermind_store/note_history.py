"""Read Git evidence in batches, and restore a missing note from its saved filing copy.

Git is only read here. No checkout, hooks, filters, remote access or ref writes
are involved. A removal Git records is the signal that a note is gone: the note's own
records are removed with it and nothing is kept back in their place.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from coppermind import frontmatter as fm
from coppermind.atomicio import create_exclusive_bytes, stage_bytes, sync_directory
from coppermind.db.models import Note
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


@dataclass(frozen=True)
class Removal:
    """A removal Git records: which note, where it was, and the commit that removed it."""

    note_id: str
    path: str
    content_hash: str
    removal_commit: str
    blob: str


# Keep each subprocess output bounded, including unrelated archived files.
_BLOB_BATCH_BYTES = 8 * 1024 * 1024
_MAX_RECOVERY_BLOB_BYTES = 32 * 1024 * 1024


class GitHistory:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.head: str | None = None
        self.key: str | None = None
        self.wanted: set[str] = set()
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

    def blobs(self, names: list[str]) -> Iterator[tuple[str, bytes]]:
        """Size-check bounded batches, never materialize a vault's blob history."""
        for start in range(0, len(names), 128):
            chunk = names[start : start + 128]
            sizes = self.git(
                "cat-file", "--batch-check", data=("\n".join(chunk) + "\n").encode()
            ).splitlines()
            batch: list[str] = []
            total = 0
            for header in sizes:
                fields = header.split()
                if len(fields) != 3 or fields[1] != b"blob":
                    raise HistoryUnavailable("Git recovery object unavailable")
                name, size = fields[0].decode(), int(fields[2])
                if batch and total + size > _BLOB_BATCH_BYTES:
                    yield from self._blob_batch(batch)
                    batch, total = [], 0
                if size > _MAX_RECOVERY_BLOB_BYTES:
                    # Unknown identity means incomplete evidence, never a
                    # license to fall back to an older version of that note.
                    raise HistoryUnavailable(
                        "A note exceeds the 32 MiB recovery limit; inspect Git manually"
                    )
                batch.append(name)
                total += size
            if batch:
                yield from self._blob_batch(batch)

    def _blob_batch(self, names: list[str]) -> Iterator[tuple[str, bytes]]:
        raw = self.git("cat-file", "--batch", data=("\n".join(names) + "\n").encode())
        offset = 0
        for name in names:
            end = raw.index(b"\n", offset)
            header = raw[offset:end].split()
            if len(header) != 3 or header[1] != b"blob":
                raise HistoryUnavailable("Git recovery object unavailable")
            size = int(header[2])
            offset = end + 1
            yield name, raw[offset : offset + size]
            offset += size + 1

    def refresh(self, key: str, enabled: bool, wanted: set[str] | None = None) -> None:
        head = None
        try:
            if not enabled:
                raise HistoryUnavailable("Git history is disabled; absent notes remain missing")
            head = self.git("rev-parse", "--verify", "HEAD").decode().strip()
            if wanted is not None and (
                head != self.head or key != self.key or not wanted.issubset(self.wanted)
            ):
                self._load(head, key, wanted)
                self.head, self.key, self.wanted = head, key, set(wanted)
            if wanted is None and not self.status.available and self.status.head == head:
                return
            self.status = HistoryStatus(
                available=True,
                head=head,
                reason="Git history readable; new removals await committed snapshots",
            )
        except (HistoryUnavailable, ValueError, UnicodeError) as exc:
            self.removed = {}
            self.head = None
            self.status = HistoryStatus(available=False, head=head, reason=str(exc))

    def _load(self, head: str, key: str, wanted: set[str]) -> None:
        # One history walk per HEAD/requested identity set, followed by bounded
        # object batches. NUL framing preserves unusual paths.
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
                mode, _, raw_blob, _, _ = field.split()
                path = next(it).decode("utf-8", "surrogateescape")
                if mode in {b":100644", b":100755"} and path.endswith(".md"):
                    removals.append((commit, raw_blob.decode(), path))
            elif re.fullmatch(rb"[0-9a-f]{40,64}", field):
                commit = field.decode()
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
        live_ids = set()
        for _, data in self.blobs(sorted(set(live_blobs))):
            identity = self.identity(data, key)
            if identity in wanted:
                live_ids.add(identity)
        outstanding = wanted - live_ids
        removed: dict[str, list[tuple[str, str, str, bytes]]] = {}
        by_blob: dict[str, list[tuple[str, str]]] = {}
        for commit, blob, path in removals:
            by_blob.setdefault(blob, []).append((commit, path))
        if outstanding:
            # Insertion order follows newest deletions first. Stop when each
            # requested identity has its newest removal; retain no unrelated bodies.
            for blob, data in self.blobs(list(by_blob)):
                note_id = self.identity(data, key)
                if note_id not in outstanding:
                    continue
                assert note_id is not None
                latest = by_blob[blob][0][0]
                removed[note_id] = [
                    (commit, blob, path, data) for commit, path in by_blob[blob] if commit == latest
                ]
                outstanding.remove(note_id)
                if not outstanding:
                    break
        self.removed = removed

    def removal_commit(self, path: str, enabled: bool, since: datetime) -> str | None:
        """The commit that removed `path` from the notes filesystem, if history records one.

        Artifact files carry no identity in their bytes, so the question is
        about the path alone: the newest first-parent commit that deleted
        exactly that path, provided HEAD no longer holds it and the commit is
        no older than `since`, when the copy was placed. A deletion of an
        earlier file at the same name is not evidence about this one. Unreadable
        or disabled history answers None, which callers read as no evidence
        rather than as absence.
        """
        if not enabled:
            return None
        try:
            held = self.git("--literal-pathspecs", "ls-tree", "-z", "HEAD", "--", path)
            if held.strip(b"\0"):
                return None
            fields = (
                self.git(
                    "--literal-pathspecs",
                    "log",
                    "--first-parent",
                    "-1",
                    "--format=%H %ct",
                    "--diff-filter=D",
                    "HEAD",
                    "--",
                    path,
                )
                .decode()
                .split()
            )
        except (HistoryUnavailable, UnicodeError):
            return None
        if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{40,64}", fields[0]):
            return None
        if not fields[1].isdigit() or int(fields[1]) < int(since.timestamp()):
            return None
        return fields[0]

    @staticmethod
    def identity(data: bytes, key: str) -> str | None:
        try:
            frontmatter, _ = fm.parse(data.decode())
            value = frontmatter.get(key)
            return value if is_valid_id(value) else None
        except (UnicodeError, fm.FrontmatterError):
            return None

    def evidence(self, note_id: str, path: str, digest: str) -> Removal | None:
        candidates = self.removed.get(note_id, [])
        # Never fall back to an older deletion generation with matching bytes.
        latest = candidates[0][0] if candidates else None
        for commit, blob, old_path, data in candidates:
            if commit != latest:
                break
            if old_path == path and content_hash(data) == digest:
                return Removal(
                    note_id=note_id,
                    path=path,
                    content_hash=digest,
                    removal_commit=commit,
                    blob=blob,
                )
        return None


def publish_receipt(path: Path, data: bytes) -> None:
    """Publish only a complete fsynced file, without replacing an existing receipt."""
    staged = stage_bytes(path, data, mode=0o600)
    try:
        os.link(staged, path)
        sync_directory(path.parent)
    finally:
        staged.unlink(missing_ok=True)


async def preview(store: LocalStore, note_id: str) -> NoteRecovery:
    """A missing note's saved filing copy, bound to this identity, path and hash."""
    async with store.session_factory() as session:
        note = await session.get(Note, note_id)
        if note is None:
            raise NotFound(note_id)
        if note.state != "missing":
            raise ValidationFailed(["Only a missing note can be restored"])
    # A verified filing copy can restore a file without asserting a Git removal.
    # Bind confirmation to this identity, path and hash, and create exclusively.
    try:
        journal = store.control.store.state_dir / "filing" / f"{note_id}.json"
        plan = json.loads(journal.read_bytes())
        copy = next(c for c in reversed(plan.get("copies", [])) if c["hash"] == note.content_hash)
        data = base64.b64decode(copy["content"], validate=True)
        if (
            content_hash(data) != note.content_hash
            or store.note_history.identity(data, store.control.schema().role("id_key")) != note_id
        ):
            raise ValueError("filing copy does not match note")
        resolve(store.notes_root, note.path)
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        raise ValidationFailed(
            [
                f"No verified saved filing copy is available for {note.path}. "
                "Restore this note from a device "
                "or backup to its previous path with the same note ID. If Git history has a "
                "matching version, check Git settings and folder access, "
                "then allow another scan."
            ]
        ) from exc
    token = hashlib.sha256(
        f"filing-restore:{note_id}:{note.path}:{note.content_hash}".encode()
    ).hexdigest()
    return NoteRecovery(
        note_id=note_id,
        path=note.path,
        content=data.decode(),
        content_hash=note.content_hash,
        removal_commit="",
        token=token,
        recorded_at=note.updated_at,
        evidence_pending=True,
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
            audit = (
                store.control.store.state_dir / "note-restores" / f"{note_id}-{request.token}.json"
            )
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
