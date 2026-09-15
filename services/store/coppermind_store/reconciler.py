"""Read note identities from the filesystem and refresh the metadata mirror.

The scan never writes a note. Files with no known identity are deliberately
ignored until the write-side reconciliation increment.

An interval scan stats every note file and reads only the ones a stat says may
have changed, so the steady-state cost is one stat per file rather than a read
and a sha256 of the whole notes filesystem every minute. The scheduled daily
rehash is the pass that reads everything, which is what catches a change a
device made without moving the file's mtime or size.

Only observed absence makes a note missing. A file the scan can see but cannot
identify, parse or open is recorded as present and unparsed, because reporting
a file the captain still has as deleted is the failure this service exists to
prevent.
"""

from __future__ import annotations

import asyncio
import re
import stat as stat_module
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from coppermind import frontmatter as fm
from coppermind.db.models import Note
from coppermind.db.session import transaction
from coppermind.logging import get_logger
from coppermind.store_protocol import MetadataUnavailable, NotesFilesystemUnavailable
from coppermind_store.fs import content_hash, resolve
from coppermind_store.notes import _jsonable, _mirror_columns, _title_of

if TYPE_CHECKING:
    from coppermind.schema import FrontmatterSchema
    from coppermind_store.notes import LocalStore

log = get_logger("coppermind-store")

_IGNORED_DIRECTORIES = {".git", ".obsidian", ".trash"}

# How many mirror rows one query materialises. The mirror is walked in keyset
# batches, and the rows a scan loads whole are only the ones it has to change.
_MIRROR_BATCH = 200

# Consecutive scans that could not complete before readiness stops calling the
# listed state current.
_WEDGED_AFTER = 3

MISSING_REASON = "not observed during reconciliation"
UNPARSED_REASON = "frontmatter could not be parsed"
UNREADABLE_REASON = "file could not be read"


@dataclass(frozen=True)
class MirrorEntry:
    """The little of a mirror row a scan needs before it opens anything."""

    note_id: str
    path: str
    state: str
    state_reason: str | None
    size_bytes: int
    mtime: datetime | None


@dataclass(frozen=True)
class Observation:
    """What one scanned file says about one known identity.

    `state` is `ok`, `unparsed` for bytes that would not parse, or `unreadable`
    for a file the scan could see but could not open. `path_derived` marks an
    identity the file did not name, inherited from the row recording its path.
    """

    note_id: str
    path: str
    state: str
    content_hash: str | None = None
    size_bytes: int | None = None
    mtime: datetime | None = None
    title: str | None = None
    frontmatter: dict[str, Any] | None = None
    path_derived: bool = False


class ReconcilerStatus:
    """Whether scans are keeping up, so readiness reports a wedged reconciler.

    A filesystem or database fault that survives several intervals stops the
    mirror converging for every note at once. Logs alone would leave the
    service answering listings from state nothing is refreshing.
    """

    def __init__(self) -> None:
        self.last_completed_at: datetime | None = None
        self.consecutive_failures = 0
        self.last_reason = ""

    def completed(self) -> None:
        self.last_completed_at = datetime.now(tz=UTC)
        self.consecutive_failures = 0
        self.last_reason = ""

    def deferred(self, reason: str) -> None:
        self.consecutive_failures += 1
        self.last_reason = reason

    def problem(self) -> str:
        """Describe a reconciler that has stopped converging, or return ''."""
        if self.consecutive_failures < _WEDGED_AFTER:
            return ""
        return (
            f"{self.consecutive_failures} scans in a row did not complete "
            f"({self.last_reason}); listed note state is not being refreshed"
        )


async def run_reconciler(store: LocalStore, status: ReconcilerStatus | None = None) -> None:
    """Run scans on the configured interval without joining a request path."""
    status = status or ReconcilerStatus()
    due: datetime | None = None
    while True:
        interval, quiet_period_s, rehash_at, zone = _cadence(store)
        await asyncio.sleep(interval)
        now = datetime.now(tz=UTC)
        full = due is not None and now >= due
        if due is None or full:
            due = _next_full_rehash(now, rehash_at, zone)
        started = monotonic()
        try:
            counts = await reconcile_once(store, full=full, quiet_period_s=quiet_period_s)
        except (MetadataUnavailable, NotesFilesystemUnavailable, OSError) as exc:
            status.deferred(type(exc).__name__)
            log.warning(
                "reconciliation deferred",
                reason=type(exc).__name__,
                consecutive=status.consecutive_failures,
            )
        except Exception as exc:  # noqa: BLE001 - a background fault must not stop the service
            status.deferred(type(exc).__name__)
            log.exception("reconciliation failed", error_type=type(exc).__name__)
        else:
            status.completed()
            log.info(
                "reconciliation completed",
                duration_ms=round((monotonic() - started) * 1000),
                full=full,
                **counts,
            )


def _cadence(store: LocalStore) -> tuple[int, int, str, ZoneInfo]:
    """The scan cadence from settings, falling back to the shipped defaults."""
    try:
        settings = store.control.settings()
        return (
            settings.reconcile.scan_interval_s,
            settings.reconcile.quiet_period_s,
            settings.reconcile.full_rehash_daily_at,
            ZoneInfo(settings.general.timezone),
        )
    except Exception as exc:  # noqa: BLE001 - readiness reports the control-file fault
        log.warning("reconciliation settings unavailable", error_type=type(exc).__name__)
        return 60, 30, "03:30", ZoneInfo("UTC")


def _next_full_rehash(after: datetime, at: str, zone: ZoneInfo) -> datetime | None:
    """The first local `HH:MM` strictly after `after`, or None if unparseable."""
    try:
        hour, minute = (int(part) for part in at.split(":", 1))
        wall_clock = time(hour=hour, minute=minute)
    except ValueError:
        log.warning("full rehash time not understood", value=at)
        return None
    local = after.astimezone(zone)
    candidate = datetime.combine(local.date(), wall_clock, tzinfo=zone)
    if candidate <= local:
        candidate = datetime.combine(local.date() + timedelta(days=1), wall_clock, tzinfo=zone)
    return candidate


async def reconcile_once(
    store: LocalStore, *, full: bool = False, quiet_period_s: int = 0
) -> dict[str, int]:
    """Make known mirror rows describe files carrying the same identity.

    `full` reads and hashes every note file. Without it a file whose size and
    mtime still match the mirror is taken at its stat. `quiet_period_s` leaves
    a file that changed within that many seconds for the next pass, so a note
    a device is still delivering is not hashed halfway through its write.
    """
    scan_started = datetime.now(tz=UTC)
    by_id = await _mirror_index(store)
    by_path = _by_path(by_id)
    schema = store.control.schema()
    settled_before = (
        scan_started - timedelta(seconds=quiet_period_s) if quiet_period_s > 0 else None
    )
    found, seen = await asyncio.to_thread(
        _scan,
        store.notes_root,
        schema,
        by_id,
        by_path,
        full=full,
        settled_before=settled_before,
    )
    observations = _choose_observations(found, by_id)
    # An identity seen on disk but not chosen (two live copies, an unchanged
    # stat, a file still settling) keeps whatever the mirror already says. Only
    # an identity nothing on disk carried is a candidate for missing.
    pending = sorted(set(observations) | {note_id for note_id in by_id if note_id not in seen})

    counts = {"changed": 0, "moved": 0, "missing": 0, "unparsed": 0}
    now = datetime.now(tz=UTC)
    try:
        async with transaction(store.session_factory) as session:
            for chunk in _chunks(pending, _MIRROR_BATCH):
                rows = (await session.scalars(sa.select(Note).where(Note.id.in_(chunk)))).all()
                for row in rows:
                    observed = observations.get(row.id)
                    values = _values_for(row, observed, schema)
                    if not values:
                        continue
                    # The update writes through to this row, so what it used to
                    # say is read before the statement rather than after it.
                    previous_path = row.path
                    values["updated_at"] = now
                    result = await session.execute(
                        sa.update(Note)
                        .where(Note.id == row.id, Note.updated_at <= scan_started)
                        .values(**values)
                        .returning(Note.id)
                    )
                    if result.scalar_one_or_none() is None:
                        continue
                    if observed is None:
                        counts["missing"] += 1
                    elif observed.state in {"unparsed", "unreadable"}:
                        counts["unparsed"] += 1
                    elif previous_path != observed.path:
                        counts["moved"] += 1
                    else:
                        counts["changed"] += 1
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
    return counts


async def _mirror_index(store: LocalStore) -> dict[str, MirrorEntry]:
    """Read the mirror's scan-relevant columns, in bounded keyset batches."""
    index: dict[str, MirrorEntry] = {}
    after: str | None = None
    columns = (Note.id, Note.path, Note.state, Note.state_reason, Note.size_bytes, Note.mtime)
    try:
        async with store.session_factory() as session:
            while True:
                statement = sa.select(*columns).order_by(Note.id).limit(_MIRROR_BATCH)
                if after is not None:
                    statement = statement.where(Note.id > after)
                rows = (await session.execute(statement)).all()
                if not rows:
                    break
                for row in rows:
                    index[row.id] = MirrorEntry(
                        note_id=row.id,
                        path=row.path,
                        state=row.state,
                        state_reason=row.state_reason,
                        size_bytes=row.size_bytes,
                        mtime=row.mtime,
                    )
                after = rows[-1].id
                if len(rows) < _MIRROR_BATCH:
                    break
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
    return index


def _by_path(by_id: dict[str, MirrorEntry]) -> dict[str, MirrorEntry]:
    """Which row currently claims each occupied path.

    A missing row's path is stale by definition, and a path two live rows share
    names neither of them, so both are left out rather than guessed at.
    """
    occupied: dict[str, MirrorEntry] = {}
    shared: set[str] = set()
    for entry in by_id.values():
        if entry.state == "missing":
            continue
        if entry.path in occupied:
            shared.add(entry.path)
        occupied[entry.path] = entry
    for path in shared:
        occupied.pop(path, None)
    return occupied


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _scan(
    root: Path,
    schema: FrontmatterSchema,
    by_id: dict[str, MirrorEntry],
    by_path: dict[str, MirrorEntry],
    *,
    full: bool,
    settled_before: datetime | None,
) -> tuple[dict[str, list[Observation]], set[str]]:
    """Walk the notes filesystem, returning what was read and what was seen."""
    try:
        if not stat_module.S_ISDIR(root.stat().st_mode):
            raise NotesFilesystemUnavailable(f"notes filesystem is not a directory: {root}")
    except OSError as exc:
        # The mount is gone. That is the one fault that must stop the whole
        # pass, because every note would otherwise look deleted at once.
        raise NotesFilesystemUnavailable(str(exc)) from exc

    observed: dict[str, list[Observation]] = {}
    seen: set[str] = set()
    try:
        for path in root.rglob("*.md"):
            relative_path = path.relative_to(root)
            if any(part in _IGNORED_DIRECTORIES for part in relative_path.parts):
                continue
            relative = relative_path.as_posix()
            entry = by_path.get(relative)
            try:
                safe_path = resolve(root, relative)
                stat_result = safe_path.stat()
            except FileNotFoundError:
                continue
            except ValueError:
                # A symlink out of the notes filesystem. Something is at the
                # path; the store just refuses to follow it.
                _record(observed, seen, _unreadable(entry, relative))
                continue
            except (OSError, RuntimeError):
                # A symlink loop reaches here as pathlib's RuntimeError rather
                # than as the ELOOP it wraps.
                _record(observed, seen, _unreadable(entry, relative))
                continue
            if not stat_module.S_ISREG(stat_result.st_mode):
                _record(observed, seen, _unreadable(entry, relative))
                continue
            mtime = datetime.fromtimestamp(stat_result.st_mtime, tz=UTC)
            if not full and entry is not None and _unchanged(entry, stat_result.st_size, mtime):
                seen.add(entry.note_id)
                continue
            if entry is not None and settled_before is not None and mtime > settled_before:
                # Deferring is only safe for a file whose identity the mirror
                # already knows. An unclaimed path has to be read, or the note
                # that moved there is recorded as deleted while it settles.
                seen.add(entry.note_id)
                continue
            try:
                data = safe_path.read_bytes()
            except FileNotFoundError:
                continue
            except OSError:
                _record(observed, seen, _unreadable(entry, relative))
                continue
            _record(
                observed,
                seen,
                _observe(safe_path, relative, data, mtime, schema, by_id, entry),
            )
    except OSError as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc
    return observed, seen


def _unchanged(entry: MirrorEntry, size_bytes: int, mtime: datetime) -> bool:
    """Whether a stat alone proves this file is the one the mirror last read.

    A row recorded from a file the scan could not open never took its size and
    mtime from that file, so it is always re-read rather than trusted.
    """
    return (
        entry.state_reason != UNREADABLE_REASON
        and entry.mtime is not None
        and entry.size_bytes == size_bytes
        and entry.mtime == mtime
    )


def _unreadable(entry: MirrorEntry | None, relative: str) -> Observation | None:
    """A file the scan could not open holds the row that names its path.

    One durable per-file fault, a mode-000 file or a symlink loop, must not
    stop every other note converging and must not report this one deleted.
    """
    if entry is None:
        return None
    return Observation(note_id=entry.note_id, path=relative, state="unreadable", path_derived=True)


def _observe(
    safe_path: Path,
    relative: str,
    data: bytes,
    mtime: datetime,
    schema: FrontmatterSchema,
    by_id: dict[str, MirrorEntry],
    entry: MirrorEntry | None,
) -> Observation | None:
    """Turn one file's bytes into what it says about a known identity."""
    text: str | None = None
    try:
        text = data.decode("utf-8")
        frontmatter, body = fm.parse(text)
    except (UnicodeDecodeError, fm.FrontmatterError):
        note_id = _identity_from_broken(text, schema)
        path_derived = note_id not in by_id
        if path_derived:
            # Nothing in the broken bytes names a note this store knows, so the
            # only claim left is the row that records this path.
            note_id = entry.note_id if entry is not None else None
        if note_id is None:
            return None
        return Observation(
            note_id=note_id,
            path=relative,
            state="unparsed",
            content_hash=content_hash(data),
            size_bytes=len(data),
            mtime=mtime,
            path_derived=path_derived,
        )
    note_id = str(frontmatter.get(schema.role("id_key"), ""))
    if note_id not in by_id:
        return None
    return Observation(
        note_id=note_id,
        path=relative,
        state="ok",
        content_hash=content_hash(data),
        size_bytes=len(data),
        mtime=mtime,
        title=_title_of(body, safe_path),
        frontmatter=_jsonable(frontmatter),
    )


def _record(
    observed: dict[str, list[Observation]], seen: set[str], observation: Observation | None
) -> None:
    if observation is None:
        return
    observed.setdefault(observation.note_id, []).append(observation)
    seen.add(observation.note_id)


def _identity_from_broken(text: str | None, schema: FrontmatterSchema) -> str | None:
    """Recover one explicit top-level identity without trusting a broken path."""
    if text is None or not text.startswith("---"):
        return None
    lines = text.splitlines()
    closing = next((index for index, line in enumerate(lines[1:], 1) if line == "---"), None)
    block = "\n".join(lines[1:closing]) if closing is not None else "\n".join(lines[1:])
    key = re.escape(schema.role("id_key"))
    matches = re.findall(rf"(?m)^{key}:[ \t]*['\"]?([^'\" \t\r\n]+)['\"]?[ \t]*$", block)
    return matches[0] if len(matches) == 1 else None


def _choose_observations(
    found: dict[str, list[Observation]], by_id: dict[str, MirrorEntry]
) -> dict[str, Observation]:
    """Pick the one file that speaks for each identity this scan saw."""
    chosen: dict[str, Observation] = {}
    for note_id, candidates in found.items():
        # A file that named this identity itself outranks one that only
        # inherited it from the row recording its path, so a stranger dropped
        # at a note's old path never captures the note that moved away.
        named = [item for item in candidates if not item.path_derived]
        ranked = named or candidates
        at_known_path = [item for item in ranked if item.path == by_id[note_id].path]
        if len(at_known_path) == 1:
            chosen[note_id] = at_known_path[0]
        elif len(ranked) == 1:
            chosen[note_id] = ranked[0]
        else:
            log.warning("duplicate note identity left unresolved", note_id=note_id)
    return chosen


def _values_for(
    row: Note, observed: Observation | None, schema: FrontmatterSchema
) -> dict[str, Any]:
    if observed is None:
        if row.state == "missing" and row.state_reason == MISSING_REASON:
            return {}
        return {"state": "missing", "state_reason": MISSING_REASON}
    if observed.state == "unreadable":
        # Nothing about the bytes is known, so only the path and the state are
        # recorded and the rest stays at its last read values.
        values: dict[str, Any] = {
            "path": observed.path,
            "state": "unparsed",
            "state_reason": UNREADABLE_REASON,
        }
    elif observed.state == "unparsed":
        values = {
            "path": observed.path,
            "content_hash": observed.content_hash,
            "size_bytes": observed.size_bytes,
            "mtime": observed.mtime,
            "state": "unparsed",
            "state_reason": UNPARSED_REASON,
        }
    else:
        assert observed.frontmatter is not None
        values = {
            "path": observed.path,
            "title": observed.title,
            "content_hash": observed.content_hash,
            "size_bytes": observed.size_bytes,
            "mtime": observed.mtime,
            "frontmatter": observed.frontmatter,
            **_mirror_columns(observed.frontmatter, schema),
            "state": "ok",
            "state_reason": None,
        }
    return {key: value for key, value in values.items() if getattr(row, key) != value}
