"""Adopt note files and refresh the metadata mirror from the filesystem.

Files with no known identity receive one through the store after they have
been quiet. The scan itself only observes bytes and proposes candidates, so
the store can perform its ordinary database-first, hash-guarded atomic write.

An interval scan stats every note file and reads only the ones a stat says may
have changed, so the steady-state cost is one stat per file rather than a read
and a sha256 of the whole notes filesystem every minute. A file carrying no
identity this store knows, which is every file in an existing tree Coppermind
was pointed at, is read once and then stat-trusted the same way, for up to
`_UNIDENTIFIED_LIMIT` such paths; past that bound the remaining unknown files
are read and parsed on every pass. The scheduled daily rehash is the pass that
reads everything, which is what catches a change a device made without moving
the file's mtime or size.

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
from datetime import UTC, date, datetime, timedelta
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

# Scan intervals that may pass with no scan completing at all before readiness
# says the same. A scan wedged inside the filesystem walk never raises, so the
# failure counter alone would stay green while nothing converged.
_STALE_INTERVALS = 3

# How long the first pass of a process has before it counts as silence. Until
# one scan has finished there is no measured runtime to size the deadline from,
# and a first pass reads and hashes the whole notes filesystem. Finite on
# purpose: a first scan that never finishes has to surface.
_FIRST_SCAN_GRACE = timedelta(minutes=30)

# How far past the walk's own clock an mtime may sit and still be read as an
# in-flight write. Sized for the skew between this container and the clock that
# stamps a network mount. Beyond it the stamp is a wrong device clock or a
# restored archive, and deferring it forever would stop every deletion being
# reported.
_CLOCK_SKEW_TOLERANCE = timedelta(seconds=60)

# The stat of a file a scan read and found no known identity in, keyed by its
# path. One process remembers this many; any beyond the bound are read every
# pass, which is correct, just not cheap.


@dataclass(frozen=True)
class UnidentifiedStat:
    size_bytes: int
    mtime: datetime
    settling: bool


UnidentifiedStats = dict[str, UnidentifiedStat]
_UNIDENTIFIED_LIMIT = 10_000

MISSING_REASON = "not observed during reconciliation"
UNPARSED_REASON = "frontmatter could not be parsed"
UNREADABLE_REASON = "file could not be read"


@dataclass(frozen=True)
class QuietWindow:
    """The mtime range that marks a file as still being delivered.

    The upper bound is read from the clock as each file is stat'd, not from
    when the pass began: a walk over a large notes filesystem takes time, and a
    file a device starts writing during that walk is the very thing the quiet
    period exists to leave alone. Past the tolerance the stamp is a wrong clock
    or a preserved archive time, not a write in progress, and treating it as
    one would hold that file back on every pass for as long as it sat there.
    """

    earliest: datetime
    tolerance: timedelta

    def holds(self, mtime: datetime) -> bool:
        return self.earliest < mtime <= datetime.now(tz=UTC) + self.tolerance


@dataclass(frozen=True)
class ScanResult:
    """What one walk of the notes filesystem learned.

    `held` are identities the walk found a file for at their own recorded path
    but produced no observation of, because it trusted the stat or left the
    file to settle. Their note is still where the mirror says it is, so a copy
    carrying the same identity elsewhere is a second live copy, never a move.

    `unidentified` is what the walk still knows about the paths that carried no
    identity this store recognises. It replaces the memory the pass was given,
    so a path that has gone is forgotten without a sweep of its own.
    """

    observed: dict[str, list[Observation]]
    seen: set[str]
    held: set[str]
    deferred: int
    unidentified: UnidentifiedStats
    adoption_candidates: list[AdoptionCandidate]
    unidentified_unparsed: int


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


@dataclass(frozen=True)
class AdoptionCandidate:
    """A quiet file the store may give an identity without losing bytes."""

    path: str
    content_hash: str
    stat_seen: UnidentifiedStat


class ReconcilerStatus:
    """Whether scans are keeping up, so readiness reports a wedged reconciler.

    A filesystem or database fault that survives several intervals stops the
    mirror converging for every note at once. Logs alone would leave the
    service answering listings from state nothing is refreshing.

    The first pass of a process is judged on a fixed grace rather than on the
    deadline, because nothing has measured how long a scan takes here yet.
    """

    def __init__(self) -> None:
        self.started_at = datetime.now(tz=UTC)
        self.last_completed_at: datetime | None = None
        self.scan_started_at: datetime | None = None
        self.longest_scan_s = 0.0
        self.consecutive_failures = 0
        self.last_reason = ""
        self.scan_interval_s = 60

    def scanning(self) -> None:
        """A scan is in flight, so its own runtime is not silence."""
        self.scan_started_at = datetime.now(tz=UTC)

    def completed(self) -> None:
        finished = datetime.now(tz=UTC)
        if self.scan_started_at is not None:
            self.longest_scan_s = max(
                self.longest_scan_s, (finished - self.scan_started_at).total_seconds()
            )
        self.scan_started_at = None
        self.last_completed_at = finished
        self.consecutive_failures = 0
        self.last_reason = ""

    def deferred(self, reason: str) -> None:
        self.scan_started_at = None
        self.consecutive_failures += 1
        self.last_reason = reason

    def problem(self) -> str:
        """Describe a reconciler that has stopped converging, or return ''."""
        if self.consecutive_failures >= _WEDGED_AFTER:
            return (
                f"{self.consecutive_failures} scans in a row did not complete "
                f"({self.last_reason}); listed note state is not being refreshed"
            )
        since = self.last_completed_at or self.started_at
        if self.scan_started_at is not None and self.scan_started_at > since:
            since = self.scan_started_at
        quiet_for = datetime.now(tz=UTC) - since
        if self.last_completed_at is None and quiet_for <= _FIRST_SCAN_GRACE:
            return ""
        # The deadline never falls below what a scan here actually takes, so a
        # long rehash, or a brisk interval an operator chose, is not a fault.
        deadline = _STALE_INTERVALS * max(self.scan_interval_s, self.longest_scan_s)
        if quiet_for <= timedelta(seconds=deadline):
            return ""
        seconds = round(quiet_for.total_seconds())
        waited = (
            f"no scan has completed in {seconds}s"
            if self.last_completed_at is None
            else f"the last scan completed {seconds}s ago"
        )
        return (
            f"{waited}, over {_STALE_INTERVALS} scan intervals; "
            "listed note state is not being refreshed"
        )


async def run_reconciler(store: LocalStore, status: ReconcilerStatus | None = None) -> None:
    """Run scans on the configured interval without joining a request path."""
    status = status or ReconcilerStatus()
    rehashed_on: date | None = None
    unidentified: UnidentifiedStats = {}
    while True:
        interval, quiet_period_s, rehash_at, zone = _cadence(store)
        status.scan_interval_s = interval
        await asyncio.sleep(interval)
        today = datetime.now(tz=zone)
        full = _rehash_due(today, rehash_at, rehashed_on)
        started = monotonic()
        status.scanning()
        try:
            counts = await reconcile_once(
                store, full=full, quiet_period_s=quiet_period_s, unidentified=unidentified
            )
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
            if full and counts["deferred"] == 0:
                # Only a rehash that ran counts for the day. A deferred one
                # stays due, because the full pass is the only thing that sees
                # a change that did not move a file's mtime or size.
                rehashed_on = today.date()
            log.info(
                "reconciliation completed",
                duration_ms=round((monotonic() - started) * 1000),
                full=full,
                **counts,
            )


def _rehash_due(now: datetime, at: str, last: date | None) -> bool:
    """Whether the daily full rehash still owes a run for `now`'s local date.

    Zero padded `HH:MM` compares as text exactly as it does as a clock, which
    the setting's own pattern guarantees. A store that starts after the hour
    rehashes on its first pass rather than waiting a day for the next one.
    """
    return last != now.date() and now.strftime("%H:%M") >= at


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


async def reconcile_once(
    store: LocalStore,
    *,
    full: bool = False,
    quiet_period_s: int = 0,
    unidentified: UnidentifiedStats | None = None,
) -> dict[str, int]:
    """Make known mirror rows describe files carrying the same identity.

    `full` reads and hashes every note file. Without it a file whose size and
    mtime still match the mirror is taken at its stat. `quiet_period_s` leaves
    a file that changed within that many seconds for the next pass, so a note
    a device is still delivering is not hashed halfway through its write.
    `unidentified` carries what earlier passes learned about files holding no
    identity this store knows, and is replaced with what this pass learned.
    """
    remembered = {} if unidentified is None else unidentified
    scan_started = datetime.now(tz=UTC)
    by_id = await _mirror_index(store)
    by_path = _by_path(by_id)
    schema = store.control.schema()
    quiet = (
        QuietWindow(
            earliest=scan_started - timedelta(seconds=quiet_period_s),
            tolerance=_CLOCK_SKEW_TOLERANCE,
        )
        if quiet_period_s > 0
        else None
    )
    scan = await asyncio.to_thread(
        _scan,
        store.notes_root,
        schema,
        by_id,
        by_path,
        full=full,
        quiet=quiet,
        unidentified=dict(remembered),
    )
    remembered.clear()
    remembered.update(scan.unidentified)
    observations = _choose_observations(scan, by_id)
    adopted = 0
    unparsed = scan.unidentified_unparsed
    adoption_deferred = 0
    for candidate in scan.adoption_candidates:
        try:
            outcome = await store.adopt_note(candidate.path, candidate.content_hash)
        except NotesFilesystemUnavailable as exc:
            # One durable per-file fault must not stop every other note
            # converging, exactly as it does not inside the scan itself. A
            # mount that is wholly gone still fails the pass from `_scan`.
            adoption_deferred += 1
            remembered.pop(candidate.path, None)
            log.warning(
                "device-created note left unchanged",
                path=candidate.path,
                reason=str(exc),
            )
            continue
        if outcome == "adopted":
            adopted += 1
            remembered.pop(candidate.path, None)
        elif outcome == "invalid":
            unparsed += 1
            _remember(remembered, candidate.path, candidate.stat_seen, settling=False)
            log.warning(
                "device-created note left unchanged",
                path=candidate.path,
                reason="frontmatter validation failed",
            )
        else:
            adoption_deferred += 1
            remembered.pop(candidate.path, None)
    # An identity seen on disk but not chosen, two live copies or an unchanged
    # stat, keeps whatever the mirror already says. Only an identity nothing on
    # disk carried is a candidate for missing, and only when this pass read
    # every file that changed: bytes left unread inside the quiet period could
    # belong to any note, so a pass that deferred one cannot call any note gone.
    deferred = scan.deferred + adoption_deferred
    absent = set() if deferred else {note_id for note_id in by_id if note_id not in scan.seen}
    pending = sorted(set(observations) | absent)

    counts = {
        "adopted": adopted,
        "changed": 0,
        "moved": 0,
        "missing": 0,
        "unparsed": unparsed,
        "deferred": deferred,
    }
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
    quiet: QuietWindow | None,
    unidentified: UnidentifiedStats,
) -> ScanResult:
    """Walk the notes filesystem and report what it found."""
    try:
        if not stat_module.S_ISDIR(root.stat().st_mode):
            raise NotesFilesystemUnavailable(f"notes filesystem is not a directory: {root}")
    except OSError as exc:
        # The mount is gone. That is the one fault that must stop the whole
        # pass, because every note would otherwise look deleted at once.
        raise NotesFilesystemUnavailable(str(exc)) from exc

    observed: dict[str, list[Observation]] = {}
    seen: set[str] = set()
    held: set[str] = set()
    still_unidentified: UnidentifiedStats = {}
    adoption_candidates: list[AdoptionCandidate] = []
    unidentified_unparsed = 0
    deferred = 0
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
                # A sync client may replace a directory entry between the
                # walk and stat. Nothing was observed absent for the whole
                # pass, so leave missing decisions until the next one.
                deferred += 1
                if entry is not None:
                    held.add(entry.note_id)
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
            mtime = datetime.fromtimestamp(stat_result.st_mtime, tz=UTC)
            settling = quiet is not None and quiet.holds(mtime)
            stat_seen = UnidentifiedStat(stat_result.st_size, mtime, settling)
            if not full and entry is not None and _unchanged(entry, stat_result.st_size, mtime):
                seen.add(entry.note_id)
                held.add(entry.note_id)
                continue
            remembered = unidentified.get(relative)
            if (
                not full
                and entry is None
                and remembered is not None
                and remembered.size_bytes == stat_seen.size_bytes
                and remembered.mtime == stat_seen.mtime
                and (not remembered.settling or settling)
            ):
                # A rejected file stays stat-trusted. A settling one is
                # opened again as soon as its mtime leaves the quiet window,
                # which is when adoption becomes safe. The stat is taken
                # before the file type is judged, so a directory or a fifo
                # named like a note is rejected once rather than every pass.
                _remember(still_unidentified, relative, stat_seen, settling=settling)
                if settling:
                    deferred += 1
                continue
            if not stat_module.S_ISREG(stat_result.st_mode):
                observation = _unreadable(entry, relative)
                _record(observed, seen, observation)
                if observation is None:
                    unidentified_unparsed += 1
                    _remember(still_unidentified, relative, stat_seen, settling=False)
                    log.warning(
                        "device-created note left unchanged",
                        path=relative,
                        reason="not a regular file",
                    )
                continue
            if entry is not None and quiet is not None and quiet.holds(mtime):
                # A file the mirror already claims is left to settle rather than
                # hashed halfway through a device's write. A file is here, so
                # the note still holds its path, but whose bytes these now are
                # is unknown until they are read: the pass is marked deferred
                # rather than vouching for the row that names the path.
                held.add(entry.note_id)
                deferred += 1
                continue
            try:
                data = safe_path.read_bytes()
            except FileNotFoundError:
                # A replace between stat and read is the same uncertainty as
                # an in-flight delivery, not evidence that the note is gone.
                deferred += 1
                if entry is not None:
                    held.add(entry.note_id)
                continue
            except OSError:
                observation = _unreadable(entry, relative)
                _record(observed, seen, observation)
                if observation is None:
                    if settling:
                        _remember(still_unidentified, relative, stat_seen, settling=True)
                        deferred += 1
                    else:
                        unidentified_unparsed += 1
                        _remember(still_unidentified, relative, stat_seen, settling=False)
                        log.warning(
                            "device-created note left unchanged",
                            path=relative,
                            reason=UNREADABLE_REASON,
                        )
                continue
            result = _observe(safe_path, relative, data, mtime, schema, by_id, entry)
            if isinstance(result, AdoptionCandidate):
                if settling:
                    _remember(still_unidentified, relative, stat_seen, settling=True)
                    deferred += 1
                else:
                    adoption_candidates.append(result)
            elif result is None:
                if settling:
                    _remember(still_unidentified, relative, stat_seen, settling=True)
                    deferred += 1
                else:
                    unidentified_unparsed += 1
                    _remember(still_unidentified, relative, stat_seen, settling=False)
                    log.warning(
                        "device-created note left unchanged",
                        path=relative,
                        reason=UNPARSED_REASON,
                    )
            else:
                _record(observed, seen, result)
    except OSError as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc
    return ScanResult(
        observed=observed,
        seen=seen,
        held=held,
        deferred=deferred,
        unidentified=still_unidentified,
        adoption_candidates=adoption_candidates,
        unidentified_unparsed=unidentified_unparsed,
    )


def _remember(
    stats: UnidentifiedStats,
    relative: str,
    stat_seen: UnidentifiedStat,
    *,
    settling: bool,
) -> None:
    """Keep this path's stat, up to the bound one process holds."""
    if len(stats) < _UNIDENTIFIED_LIMIT:
        stats[relative] = UnidentifiedStat(stat_seen.size_bytes, stat_seen.mtime, settling)


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
) -> Observation | AdoptionCandidate | None:
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
        if entry is None:
            # The scan only proposes a file whose mtime has already left the
            # quiet window, so the candidate it carries is never settling.
            return AdoptionCandidate(
                path=relative,
                content_hash=content_hash(data),
                stat_seen=UnidentifiedStat(len(data), mtime, False),
            )
        # The file is still at a known path, and its parsed content does not
        # explicitly name another known note. Keep that row present until a
        # later edit restores an identity or the file is observed absent.
        return Observation(
            note_id=entry.note_id,
            path=relative,
            state="unparsed",
            content_hash=content_hash(data),
            size_bytes=len(data),
            mtime=mtime,
            path_derived=True,
        )
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


def _choose_observations(scan: ScanResult, by_id: dict[str, MirrorEntry]) -> dict[str, Observation]:
    """Pick the one file that speaks for each identity this scan saw."""
    chosen: dict[str, Observation] = {}
    for note_id, candidates in scan.observed.items():
        if note_id in scan.held:
            # The walk found this note's own file still at its recorded path,
            # so whatever else carries the identity is a second live copy.
            # Neither is chosen over the other.
            log.warning("duplicate note identity left unresolved", note_id=note_id)
            continue
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
