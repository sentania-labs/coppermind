"""Adopt note files and refresh the metadata mirror from the filesystem.

Files with no known identity receive one through the store after they have
been quiet. The scan itself only observes bytes and proposes candidates, so
the store can perform its ordinary database-first, hash-guarded atomic write.
A pass adopts at most `_ADOPTIONS_PER_PASS` files and leaves the rest for the
next one. A candidate the store refused, or could not write, keeps its stat
like any other rejected file, so cheap passes stop sweeping it and the daily
thorough rehash is what tries it again.

An interval scan stats every note file and reads only the ones a stat says may
have changed, so the steady-state cost is one stat per file rather than a read
and a sha256 of the whole notes filesystem every minute. A file carrying no
identity this store knows, which is every file in an existing tree Coppermind
was pointed at, is read once and then stat-trusted the same way, for up to
`_UNIDENTIFIED_LIMIT` such paths; past that bound the remaining unknown files
are read and parsed on every pass. Excluded files are always reread so a rule
change can take effect next pass and their identities can still be followed.
The scheduled daily rehash is the pass that
reads everything, which is what catches a change a device made without moving
the file's mtime or size.

Only observed absence makes a note missing. A file the scan can see but cannot
identify, parse or open is recorded as present and unparsed, because reporting
a file the captain still has as deleted is the failure this service exists to
prevent.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import stat as stat_module
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from coppermind import frontmatter as fm
from coppermind.db.models import Note, RecordedRejection
from coppermind.db.session import transaction
from coppermind.logging import get_logger
from coppermind.naming import sanitize_folder
from coppermind.settings import ProductSettings
from coppermind.store_protocol import MetadataUnavailable, NotesFilesystemUnavailable
from coppermind_store import indexer
from coppermind_store.fs import content_hash, resolve
from coppermind_store.maintenance import writer
from coppermind_store.notes import _jsonable, _mirror_columns, _title_of
from coppermind_store.notices import clear_path_notice
from coppermind_store.rejections import reconcile_rejections

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
# pass, which is correct, just not cheap. Only a durable rejection is kept: a
# file still inside the quiet period is simply read again next pass, so a notes
# filesystem arriving all at once cannot fill the bound with paths that are
# about to settle.
UnidentifiedStats = dict[str, tuple[int, datetime]]
_UNIDENTIFIED_LIMIT = 10_000

# How many device-created files one pass gives an identity to. Pointing the
# store at a notes filesystem that already holds a person's notes makes every
# file in it a candidate at once, and each adoption is its own database
# transaction and a rewritten file that Obsidian Sync then pushes to every
# device. The bound spreads that first adoption over successive passes rather
# than one burst, and costs nothing: a file left over is a candidate again on
# the next pass. It counts files actually adopted, not files tried, so a
# subtree that cannot be written never consumes the whole bound and starves
# the rest of the notes filesystem of its turn.
_ADOPTIONS_PER_PASS = 50

MISSING_REASON = "not observed during reconciliation"
DELETED_REASON = "removal recorded in Git; recovery copy retained"
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
    exclusions: dict[str, str] = field(default_factory=dict)


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
    body: str | None = None
    path_derived: bool = False


@dataclass(frozen=True)
class AdoptionCandidate:
    """A quiet file the store may give an identity without losing bytes."""

    path: str
    content_hash: str
    stat_seen: tuple[int, datetime]


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
            await _index_after_pass(store)
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


async def _index_after_pass(store: LocalStore) -> None:
    """Catch the search index up with the mirror this pass left behind.

    The index is derived, so a fault here is logged and retried next pass; it
    never counts against reconciliation, which is what readiness watches.
    """
    try:
        from coppermind_store.filing import file_reviewed

        await file_reviewed(store)
    except Exception as exc:  # noqa: BLE001 - filing must not stop the reconciler
        log.warning("filing deferred", reason=type(exc).__name__)
    try:
        from coppermind_store.artifacts import sweep

        swept = await sweep(store)
    except Exception as exc:  # noqa: BLE001 - the sweep must not stop the reconciler
        log.warning("artifact sweep deferred", reason=type(exc).__name__)
    else:
        if any(swept.values()):
            log.info("artifacts swept", **swept)
    try:
        counts = await indexer.after_pass(store)
    except Exception as exc:  # noqa: BLE001 - the index must not stop the reconciler
        log.warning("search index catch-up deferred", reason=type(exc).__name__)
        return
    if any(
        counts.get(key)
        for key in ("indexed", "removed", "unreadable", "notes", "sources", "artifacts")
    ):
        log.info("search index caught up", **counts)


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


@writer
async def reconcile_once(
    store: LocalStore,
    *,
    full: bool = False,
    quiet_period_s: int = 0,
    unidentified: UnidentifiedStats | None = None,
    locked: bool = False,
) -> dict[str, int]:
    """Make known mirror rows describe files carrying the same identity.

    `full` reads and hashes every note file. Without it a file whose size and
    mtime still match the mirror is taken at its stat. `quiet_period_s` leaves
    a file that changed within that many seconds for the next pass, so a note
    a device is still delivering is not hashed halfway through its write.
    `unidentified` carries what earlier passes learned about files holding no
    identity this store knows, and is replaced with what this pass learned.
    """
    if not locked:
        async with store._reconcile_lock:
            return await reconcile_once(
                store,
                full=full,
                quiet_period_s=quiet_period_s,
                unidentified=unidentified,
                locked=True,
            )
    from coppermind_store import note_history

    await note_history.recover_rows(store)
    await reconcile_rejections(store.control.store.state_dir, store.session_factory)
    remembered = {} if unidentified is None else unidentified
    scan_started = datetime.now(tz=UTC)
    from coppermind_store.purge import pending_ids

    purging = pending_ids(store, "note")
    by_id = {k: v for k, v in (await _mirror_index(store)).items() if k not in purging}
    by_path = _by_path(by_id)
    schema_doc = store.control.schema_document()
    schema = schema_doc.frontmatter_schema
    revision = schema_doc.revision
    settings = store.control.settings()
    quiet = (
        QuietWindow(
            earliest=scan_started - timedelta(seconds=quiet_period_s),
            tolerance=_CLOCK_SKEW_TOLERANCE,
        )
        if quiet_period_s > 0
        else None
    )
    # A refused file is not read again until it changes, so a corrected schema
    # has to make every remembered refusal worth reading afresh.
    if getattr(store, "_schema_revision_seen", revision) != revision:
        remembered.clear()
    store._schema_revision_seen = revision
    scan = await asyncio.to_thread(
        _scan,
        store.notes_root,
        schema,
        by_id,
        by_path,
        full=full,
        quiet=quiet,
        unidentified=dict(remembered),
        unadoptable=_unadoptable_folders(settings),
        excluded_folders=settings.reconcile.excluded_folders,
        excluded_patterns=settings.reconcile.excluded_patterns,
    )
    remembered.clear()
    remembered.update(scan.unidentified)
    collisions_list: list[tuple[str, str, str, str]] = []
    unresolved: dict[str, list[str]] = {}
    leftovers: dict[str, _Leftovers] = {}
    observations, duplicates = _choose_observations(
        scan,
        by_id,
        collisions_list,
        unresolved,
        leftovers,
        sanitize_folder(settings.notes.review_folder),
    )
    duplicates += await _settle_leftovers(store, leftovers, collisions_list, unresolved)
    _log_unresolved(store, unresolved)
    adopted = 0
    rejected = scan.unidentified_unparsed
    unwritable = 0
    retry = 0
    untried = iter(scan.adoption_candidates)
    refusals: dict[str, str] = {}
    judged: set[str] = set()
    for candidate in untried:
        # Every refusal inside `adopt_note` returns before its first await, so
        # a tree the schema mostly refuses would sweep end to end without the
        # event loop ever getting a turn. One yield per candidate keeps reads
        # and readiness served while a first adoption works through a notes
        # filesystem.
        await asyncio.sleep(0)
        try:
            outcome, cause = await store.adopt_note(
                candidate.path, candidate.content_hash, schema=schema, settings=settings
            )
        except MetadataUnavailable:
            # The mirror is gone, which is every note's problem, not this
            # file's. It fails the pass the same way a missing mount does.
            raise
        except Exception as exc:  # noqa: BLE001 - one file must not wedge the pass
            # One durable per-file fault must not stop every other note
            # converging, exactly as it does not inside the scan itself. The
            # stat is kept so an unwritable subtree is not swept on every cheap
            # pass; the daily thorough scan reads it again regardless and is
            # what retries it. The type is the whole reason: an exception
            # message can quote the file, and logs are collected and shipped.
            unwritable += 1
            _remember(remembered, candidate.path, candidate.stat_seen)
            log.warning(
                "device-created note not adopted",
                path=candidate.path,
                reason=type(exc).__name__,
            )
            continue
        judged.add(candidate.path)
        if outcome == "adopted":
            adopted += 1
            if adopted == _ADOPTIONS_PER_PASS:
                break
        elif outcome == "changed":
            # The bytes moved under the read. Nothing is wrong with the file,
            # so it is read afresh and proposed again on the next pass.
            retry += 1
            log.info("device-created note not adopted", path=candidate.path, reason=cause)
        else:
            rejected += 1
            refusals[candidate.path] = cause
            _remember(remembered, candidate.path, candidate.stat_seen)
            log.warning("device-created note left unchanged", path=candidate.path, reason=cause)
    backlog = retry + sum(1 for _ in untried)
    # An identity seen on disk but not chosen, two live copies or an unchanged
    # stat, keeps whatever the mirror already says. Only an identity nothing on
    # disk carried is a candidate for missing, and only when this pass read
    # every file that changed: bytes left unread inside the quiet period could
    # belong to any note, so a pass that deferred one cannot call any note gone.
    # A candidate left unadopted is not such a file: its bytes were read and
    # name no identity the mirror knows, so holding it over for the next pass
    # cannot make any known note look absent, and must not stop one being
    # reported gone or hold the daily rehash due for as long as the fault lasts.
    absent = set() if scan.deferred else {note_id for note_id in by_id if note_id not in scan.seen}
    if absent:
        await asyncio.to_thread(
            store.note_history.refresh, schema.role("id_key"), settings.git.enabled, absent
        )
    pending = sorted(set(observations) | absent)

    counts = {
        "adopted": adopted,
        "backlog": backlog,
        "rejected": rejected,
        "unwritable": unwritable,
        "duplicates": duplicates,
        "changed": 0,
        "moved": 0,
        "missing": 0,
        "deleted": 0,
        "unparsed": 0,
        "deferred": scan.deferred,
        "excluded": len(scan.exclusions),
    }
    now = datetime.now(tz=UTC)
    try:
        async with transaction(store.session_factory) as session:
            # Collisions are recomputed on every pass, so the recorded set is
            # always the one this scan saw; nothing is written into a note.
            await session.execute(
                sa.delete(RecordedRejection).where(RecordedRejection.kind == "collision")
            )

            # A refused file is not looked at again until it changes, so its
            # row stays while the file is still remembered and goes when the
            # file was judged again this pass, was adopted, or is gone.
            stale = sa.delete(RecordedRejection).where(RecordedRejection.kind == "adoption")
            kept = set(remembered) - judged
            if kept:
                stale = stale.where(RecordedRejection.reference.not_in(kept))
            await session.execute(stale)
            for path, reason in refusals.items():
                action, text = adoption_problem(reason)
                session.add(
                    RecordedRejection(
                        kind="adoption",
                        reference=path,
                        reason=f"{action}\n{text}",
                        created_at=now,
                    )
                )

            for note_id, _path, kind, reason in collisions_list:
                session.add(
                    RecordedRejection(
                        kind=kind,
                        reference=note_id,
                        reason=reason,
                        created_at=now,
                    )
                )

            for chunk in _chunks(pending, _MIRROR_BATCH):
                rows = (await session.scalars(sa.select(Note).where(Note.id.in_(chunk)))).all()
                for row in rows:
                    observed = observations.get(row.id)
                    retired = (
                        store.context.read("retired-recordings/" + row.id, {})
                        if observed is None
                        else {}
                    )
                    if retired.get("snapshot") and retired.get("source_id"):
                        if row.state != "deleted" or row.state_reason != "Merged into another note":
                            result = await session.execute(
                                sa.update(Note)
                                .where(Note.id == row.id, Note.updated_at <= scan_started)
                                .values(
                                    state="deleted",
                                    state_reason="Merged into another note",
                                    updated_at=now,
                                )
                                .returning(Note.id)
                            )
                            if result.scalar_one_or_none() is not None:
                                await indexer.drop_note(session, row.id, now)
                                counts["deleted"] += 1
                        continue
                    values = _values_for(row, observed, schema)
                    receipt = None
                    if observed is None:
                        receipt = store.note_history.evidence(row.id, row.path, row.content_hash)
                        if receipt is None:
                            from coppermind_store.filing import vanished_receipt

                            receipt, reason = vanished_receipt(store, row)
                            if receipt is None and reason:
                                values = {
                                    key: value
                                    for key, value in {
                                        "state": "missing",
                                        "state_reason": reason,
                                    }.items()
                                    if getattr(row, key) != value
                                }
                        if receipt is not None:
                            values = {
                                "state": "deleted",
                                "state_reason": DELETED_REASON,
                            }
                            retained = note_history.directory(store) / (
                                f"{row.id}-{receipt.removal_commit}.json"
                            )
                            if row.state == "deleted" and retained.is_file():
                                values = {}
                        elif row.state == "deleted":
                            candidates = store.note_history.removed.get(row.id, [])
                            if candidates:
                                # A return and a newer deletion can both occur
                                # between scans. Never offer the older contents.
                                _, _, latest_path, data = candidates[0]
                                receipt = store.note_history.evidence(
                                    row.id, latest_path, content_hash(data)
                                )
                                frontmatter, body = fm.parse(data.decode())
                                values = {
                                    "path": latest_path,
                                    "content_hash": content_hash(data),
                                    "size_bytes": len(data),
                                    "title": _title_of(body, Path(latest_path)),
                                    "frontmatter": _jsonable(frontmatter),
                                    **_mirror_columns(frontmatter, schema),
                                    "state": "deleted",
                                    "state_reason": DELETED_REASON,
                                }
                            elif not store.note_history.status.available:
                                # A Git outage cannot erase retained evidence.
                                values = {}
                            # Readable Git with no current removal leaves the
                            # note missing, awaiting the next committed snapshot.
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
                    clear_path_notice(store, row.id)
                    if receipt is not None:
                        await asyncio.to_thread(note_history.save, store, receipt)
                    elif observed is not None:
                        await asyncio.to_thread(note_history.remember_return, store, row, values)
                    if (
                        "reviewed" in values
                        and observed is not None
                        and observed.frontmatter is not None
                    ):
                        from coppermind_store.filing import record_review

                        record_review(store, row.id, observed.frontmatter)
                    await _follow_in_index(
                        session, row.id, observed, now, settings.notes.notes_folder
                    )
                    if observed is None:
                        counts["deleted" if receipt is not None else "missing"] += 1
                    elif observed.state in {"unparsed", "unreadable"}:
                        counts["unparsed"] += 1
                    elif previous_path != observed.path:
                        counts["moved"] += 1
                    else:
                        counts["changed"] += 1
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
    store.control.write_reconciler_exclusions(scan.exclusions)
    try:
        await store.entities.refresh()
    except Exception as exc:  # noqa: BLE001 - dossiers must not stop note reconciliation
        log.warning("dossier refresh deferred", reason=type(exc).__name__)
    return counts


async def _follow_in_index(
    session: AsyncSession,
    note_id: str,
    observed: Observation | None,
    now: datetime,
    notes_folder: str,
) -> None:
    """Make the search index say what the mirror row now says, in its transaction.

    A note observed gone leaves the index. One read whole is indexed from the
    bytes this pass read. One that could not be parsed or opened keeps the
    text it was last indexed with, at the path it is now found at, so a broken
    edit on a device does not make a note unfindable.
    """
    if observed is None:
        await indexer.drop_note(session, note_id, now)
    elif observed.state == "ok" and observed.body is not None:
        assert observed.content_hash is not None
        await indexer.index_note(
            session,
            note_id=note_id,
            path=observed.path,
            title=observed.title or Path(observed.path).stem,
            body=observed.body,
            version=observed.content_hash,
            now=now,
            notes_folder=notes_folder,
        )
    else:
        await indexer.follow_note_path(session, note_id, observed.path)


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
        if entry.state in {"missing", "deleted"}:
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
    unadoptable: frozenset[tuple[str, ...]],
    excluded_folders: list[str],
    excluded_patterns: list[str],
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
    exclusions: dict[str, str] = {}
    try:
        for path in _walk(root):
            relative_path = path.relative_to(root)
            if any(part in _IGNORED_DIRECTORIES for part in relative_path.parts):
                continue
            relative = relative_path.as_posix()
            entry = by_path.get(relative)
            path_rule = _path_exclusion(relative_path, excluded_folders, excluded_patterns)
            markdown = path.suffix == ".md"
            if not markdown and path.suffix != ".canvas" and not path_rule and entry is None:
                continue
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
                if entry is None:
                    deferred += 1
                continue
            except (OSError, RuntimeError):
                # A symlink loop reaches here as pathlib's RuntimeError rather
                # than as the ELOOP it wraps.
                _record(observed, seen, _unreadable(entry, relative))
                if entry is None:
                    deferred += 1
                continue
            if stat_module.S_ISDIR(stat_result.st_mode) and not markdown and entry is None:
                continue
            mtime = datetime.fromtimestamp(stat_result.st_mtime, tz=UTC)
            settling = quiet is not None and quiet.holds(mtime)
            stat_seen = (stat_result.st_size, mtime)
            if not full and entry is not None and _unchanged(entry, stat_result.st_size, mtime):
                seen.add(entry.note_id)
                held.add(entry.note_id)
                continue
            if (
                not full
                and entry is None
                and not path_rule
                and unidentified.get(relative) == stat_seen
            ):
                # Read once already, and it named no note this store knows.
                # Nothing but a change to the file itself can make it one. The
                # stat is taken before the file type is judged, so a directory
                # or a fifo named like a note is rejected once, not every pass.
                _remember(still_unidentified, relative, stat_seen)
                if settling:
                    deferred += 1
                continue
            if not stat_module.S_ISREG(stat_result.st_mode):
                observation = _unreadable(entry, relative)
                _record(observed, seen, observation)
                if observation is None:
                    deferred += 1
                    unidentified_unparsed += 1
                    log.warning(
                        "device-created note left unchanged",
                        path=relative,
                        reason="not a regular file",
                    )
                continue
            if entry is not None and settling:
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
                    deferred += 1
                    if not settling:
                        unidentified_unparsed += 1
                        log.warning(
                            "device-created note left unchanged",
                            path=relative,
                            reason=UNREADABLE_REASON,
                        )
                continue
            result = _observe(safe_path, relative, data, mtime, schema, by_id, entry)
            # Identity observation always wins over an adoption exclusion, even
            # for a broken note whose known identity can still be recovered.
            rule = path_rule or _frontmatter_exclusion(data)
            if not isinstance(result, Observation) and rule:
                exclusions[relative] = rule
                if settling:
                    deferred += 1
                # Do not stat-cache exclusions: removing a rule must permit
                # adoption on the next pass, and each pass reports the full set.
                continue
            if not markdown and not isinstance(result, Observation):
                continue
            if isinstance(result, AdoptionCandidate):
                if settling:
                    deferred += 1
                elif relative_path.parts[0].casefold() in {"people", "companies"} or any(
                    relative_path.parts[: len(folder)] == folder for folder in unadoptable
                ):
                    # Read store-owned folders too, to follow known identities.
                    _remember(still_unidentified, relative, stat_seen)
                else:
                    adoption_candidates.append(result)
            elif result is None:
                if settling:
                    deferred += 1
                else:
                    unidentified_unparsed += 1
                    _remember(still_unidentified, relative, stat_seen)
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
        exclusions=exclusions,
    )


def _path_exclusion(path: Path, folders: list[str], patterns: list[str]) -> str | None:
    """Return the first matching rule, with folders relative to the notes root."""
    for folder in folders:
        parts = tuple(folder.rstrip("/").split("/"))
        if path.parts[: len(parts)] == parts:
            return f"folder: {folder.rstrip('/')}/"
    for pattern in patterns:
        target = path.as_posix() if "/" in pattern else path.name
        if fnmatch.fnmatchcase(target, pattern):
            return f"pattern: {pattern}"
    return None


def _unadoptable_folders(settings: ProductSettings) -> frozenset[tuple[str, ...]]:
    """The folders the store writes, so adoption never writes there.

    Deleted notes, generated source projections, artifacts and the legacy
    attachments folder. An artifact is never a note, so a Markdown file placed
    under the artifacts folder is never adopted. Their names are the
    operator's, read from the same settings the rest of the system lays the
    notes filesystem out by, and each is split into its segments because a
    name may nest: `Archive/Trash` has to exclude what is under it, not every
    path that merely starts with `Archive`.
    """
    named = (
        settings.notes.trash_folder,
        settings.notes.sources_folder,
        settings.notes.artifacts_folder,
        settings.notes.attachments_folder,
        "People",
        "Companies",
    )
    return frozenset(
        parts
        for parts in (tuple(part for part in name.split("/") if part) for name in named)
        if parts
    )


def _frontmatter_exclusion(data: bytes) -> str | None:
    """Plugin marker keys identify plugin files regardless of their value."""
    try:
        text = data.decode("utf-8")
        frontmatter, _ = fm.parse(text)
    except (UnicodeDecodeError, fm.FrontmatterError):
        return None
    from coppermind_store.entities import MARKER

    if MARKER.search(text):
        return "dossier marker"
    for marker in ("excalidraw-plugin", "kanban-plugin"):
        if marker in frontmatter:
            return f"frontmatter: {marker}"
    return None


def _remember(stats: UnidentifiedStats, relative: str, stat_seen: tuple[int, datetime]) -> None:
    """Keep this path's stat, up to the bound one process holds."""
    if len(stats) < _UNIDENTIFIED_LIMIT:
        stats[relative] = stat_seen


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
            return AdoptionCandidate(
                path=relative,
                content_hash=content_hash(data),
                stat_seen=(len(data), mtime),
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
        body=body,
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


@dataclass(frozen=True)
class _Leftovers:
    """Copies of a note's identity sitting in the review folder after it was filed.

    `keeper_path` is where the note's own file is recorded. `keeper` is what this
    pass read of it, absent when the walk trusted its stat.
    """

    keeper_path: str
    keeper: Observation | None
    copies: list[Observation]


def _clause(prefix: str, cause: str) -> list[str]:
    found = re.search(re.escape(prefix) + r"([^;]+)", cause)
    if found is None:
        return []
    return [name.strip() for name in found.group(1).split(",")]


def _first_key(prefix: str, cause: str) -> str | None:
    found = re.search(re.escape(prefix) + r"([^;,]+)", cause)
    return found.group(1).strip() if found else None


_CHECKS_AGAIN = "Coppermind checks again on its own."
_UNCHANGED = "Coppermind left this note unchanged"
_FRONTMATTER_CATEGORIES = {
    "unterminated_block": (
        "the properties block at the top is never closed. Add a line with three dashes "
        "(---) after the last property"
    ),
    "not_a_mapping": (
        "the properties block at the top is not a list of name: value lines. Rewrite it "
        "that way, or remove it"
    ),
    "unsupported_line_endings": (
        "its line endings aren't supported. Open it in a text editor and save it with "
        "standard line endings (LF or CRLF)"
    ),
}


def adoption_problem(cause: str) -> tuple[str, str]:
    """What the Problems page tells a person about a note adoption refused.

    The cause is written for logs, in the store's own words. This is the same
    fact for the person who owns the note: that it was left unchanged, why, and
    what to do about it, in the names the Admin navigation uses. The first value
    is the action Admin offers beside it. Every cause gets a plain sentence,
    including one this function has never seen, and none ever quotes a code.
    """
    from coppermind_store import notes as causes

    parts: list[str] = []
    action = "obsidian"
    default = _clause(causes.CLAUSE_DEFAULT, cause)
    if default:
        listed = " and ".join(default)
        own = _first_key(causes.CLAUSE_DEFAULT, cause) if len(default) == 1 else None
        return (
            f"property:{own}" if own else "defaults",
            f"{_UNCHANGED}. The note has no {listed} yet, and the default Coppermind "
            "would fill in is not one of the allowed choices. Change the default in Tags "
            "and advanced rules, or add it to the allowed choices in Note properties. "
            "Coppermind checks again on its own after you save the change.",
        )
    missing = _clause(causes.CLAUSE_MISSING, cause)
    if missing:
        parts.append(
            f"This note needs {_a(missing)}. Add "
            f"{'it' if len(missing) == 1 else 'them'} as "
            f"{'a property' if len(missing) == 1 else 'properties'} in Obsidian and save "
            "the note."
        )
    choices = _clause(causes.CLAUSE_REFUSED_CHOICE, cause)
    if choices:
        action = "properties"
        parts.append(
            f"Its {' and '.join(choices)} is not an allowed choice. Open the note in "
            "Obsidian and choose an allowed one (the choices are listed in Note "
            "properties)."
        )
    wrong = _clause(causes.CLAUSE_REFUSED_VALUE, cause)
    if wrong:
        parts.append(
            f"Its {' and '.join(wrong)} holds a value of the wrong kind. Open the note "
            "in Obsidian and correct it."
        )
    if parts:
        return action, f"{_UNCHANGED}. {' '.join(parts)} {_CHECKS_AGAIN}"
    if cause == causes.CAUSE_BAD_ID:
        return (
            "obsidian",
            "Coppermind could not identify this note, so it left it unchanged. The "
            "note's identifier property is not in a form Coppermind can use. Open the "
            "note in Obsidian and delete that property. Coppermind gives the note a new "
            "identifier on its next check, and the note itself stays as it is.",
        )
    reasons = {
        causes.CAUSE_OUTSIDE: (
            "its location is outside the notes folder. Move it into the notes folder in Obsidian"
        ),
        causes.CAUSE_NOT_UTF8: (
            "its text is not saved as UTF-8. Open it in a text editor and save it as UTF-8"
        ),
        causes.CAUSE_GENERATED: (
            "Coppermind produced this file itself from a source, so it is not a note "
            "to add. Leave it where it is"
        ),
        causes.CAUSE_UNSTORABLE: (
            "one of its properties holds a value Coppermind cannot keep. Open the note "
            "in Obsidian and simplify that property"
        ),
        causes.CAUSE_ID_TAKEN: (
            "another note already uses the same identifier, which usually means this "
            "is a copy. Delete the copy, or delete the identifier property from it and "
            "Coppermind will give it a new one"
        ),
        causes.CAUSE_PATH_TAKEN: (
            "another note is already recorded at this location. Rename this file in Obsidian"
        ),
    }
    if cause in reasons:
        return "obsidian", f"{_UNCHANGED} because {reasons[cause]}. {_CHECKS_AGAIN}"
    if cause in _FRONTMATTER_CATEGORIES:
        return "obsidian", f"{_UNCHANGED} because {_FRONTMATTER_CATEGORIES[cause]}. {_CHECKS_AGAIN}"
    return (
        "obsidian",
        f"{_UNCHANGED} because it could not be read as a note. Open it in Obsidian, "
        f"check the properties block at the top of the file, and save it. {_CHECKS_AGAIN}",
    )


def _a(names: list[str]) -> str:
    if len(names) == 1:
        return f"{'an' if names[0][:1].lower() in 'aeiou' else 'a'} {names[0]}"
    return " and ".join(names)


def _in_folder(path: str, folder: str) -> bool:
    return bool(folder) and path.startswith(folder + "/")


def _is_leftover_group(keeper_path: str, copies: list[Observation], folder: str) -> bool:
    return (
        bool(copies)
        and not _in_folder(keeper_path, folder)
        and all(_in_folder(item.path, folder) for item in copies)
    )


def _choose_observations(
    scan: ScanResult,
    by_id: dict[str, MirrorEntry],
    collisions: list[tuple[str, str, str, str]],
    unresolved: dict[str, list[str]],
    leftovers: dict[str, _Leftovers],
    review_folder: str,
) -> tuple[dict[str, Observation], int]:
    """Pick the one file that speaks for each identity this scan saw.

    An identity two live files claim is left alone: neither is chosen, neither
    is changed, and neither is reported gone. The count comes back with the
    choices so the pass reports it where an operator already reads what it
    would not take on, rather than leaving it in a log line nothing counts.

    A copy sitting in the review folder beside a note whose own file is
    elsewhere is a possible leftover of filing, not an ordinary collision, and
    comes back in `leftovers` for the pass to settle.
    """
    chosen: dict[str, Observation] = {}
    duplicates = 0
    for note_id, candidates in scan.observed.items():
        known_path = by_id[note_id].path
        if note_id in scan.held:
            # The walk found this note's own file still at its recorded path,
            # so whatever else carries the identity is a second live copy. That
            # file produced no observation of its own, so its recorded path is
            # named here to report both sides of the collision.
            if _is_leftover_group(known_path, candidates, review_folder):
                leftovers[note_id] = _Leftovers(known_path, None, list(candidates))
                continue
            duplicates += 1
            paths = [known_path, *(item.path for item in candidates)]
            unresolved[note_id] = paths
            collisions.extend(
                [(note_id, p, "collision", f"{p} carries this note identity") for p in paths]
            )
            continue
        # A file that named this identity itself outranks one that only
        # inherited it from the row recording its path, so a stranger dropped
        # at a note's old path never captures the note that moved away.
        named = [item for item in candidates if not item.path_derived]
        ranked = named or candidates
        at_known_path = [item for item in ranked if item.path == known_path]
        if len(at_known_path) == 1:
            chosen[note_id] = at_known_path[0]
            others = [item for item in ranked if item is not at_known_path[0]]
            if _is_leftover_group(known_path, others, review_folder):
                leftovers[note_id] = _Leftovers(known_path, at_known_path[0], others)
        elif len(ranked) == 1:
            chosen[note_id] = ranked[0]
        else:
            duplicates += 1
            paths = [item.path for item in ranked]
            unresolved[note_id] = paths
            collisions.extend(
                [(note_id, p, "collision", f"{p} carries this note identity") for p in paths]
            )
    return chosen, duplicates


def _journal_hashes(store: LocalStore, note_id: str) -> set[str]:
    """Every hash filing recorded for this note's contents, in flight or settled."""
    path = store.control.store.state_dir / "filing" / f"{note_id}.json"
    try:
        plan = json.loads(path.read_bytes())
        recorded = {h for h in plan.get("hashes", []) if isinstance(h, str)}
        recorded.update(
            copy["hash"]
            for copy in plan.get("copies", [])
            if isinstance(copy, dict) and isinstance(copy.get("hash"), str)
        )
        return recorded
    except (OSError, ValueError, AttributeError, TypeError):
        return set()


def _through_symlink(root: Path, relative: str) -> bool:
    """Whether the file, or any folder on the way to it, is a symbolic link."""
    current = root
    for part in Path(relative).parts:
        current = current / part
        try:
            if stat_module.S_ISLNK(current.lstat().st_mode):
                return True
        except OSError:
            return False
    return False


def _remove_verified(
    root: Path, relative: str, review_folder: str, expected: str, keeper_relative: str
) -> bool:
    """Delete a review copy only if it is provably a separate file with the judged bytes.

    Nothing here follows a link: the copy must be a regular file reached
    through no symbolic link, inside the review folder, and a different file
    (by device and inode) from the note's own, so a link or hard link to the
    filed note can never make this delete the note itself.
    """
    parts = Path(relative).parts
    if not _in_folder(relative, review_folder) or ".." in parts or Path(relative).is_absolute():
        return False
    if _through_symlink(root, relative):
        return False
    target = root / relative
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        before = target.lstat()
        if not stat_module.S_ISREG(before.st_mode):
            return False
        descriptor = os.open(target, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                return False
            try:
                keeper = (root / keeper_relative).stat()
            except FileNotFoundError:
                return False
            if (opened.st_dev, opened.st_ino) == (keeper.st_dev, keeper.st_ino):
                return False
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                data = handle.read()
        finally:
            os.close(descriptor)
        if content_hash(data) != expected:
            return False
        target.unlink()
    except FileNotFoundError:
        return True
    except (OSError, ValueError, RuntimeError):
        return False
    return True


async def _settle_leftovers(
    store: LocalStore,
    leftovers: dict[str, _Leftovers],
    collisions: list[tuple[str, str, str, str]],
    unresolved: dict[str, list[str]],
) -> int:
    """Remove review copies of filed notes that hold nothing the note lacks.

    A copy goes only when its bytes are exactly the filed note's, or exactly a
    snapshot filing recorded of it before the move, so nothing is lost. A copy
    that differs is never touched: it is reported with both paths instead.
    Returns how many identities still have a copy left in place.
    """
    if not leftovers:
        return 0
    try:
        async with store.session_factory() as session:
            rows = {
                row.id: row
                for row in (
                    await session.execute(
                        sa.select(Note.id, Note.filed, Note.content_hash).where(
                            Note.id.in_(sorted(leftovers))
                        )
                    )
                ).all()
            }
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
    unsettled = 0
    review_folder = sanitize_folder(store.control.settings().notes.review_folder)
    for note_id, group in leftovers.items():
        row = rows.get(note_id)
        filed = row is not None and bool(row.filed)
        known: set[str] = set()
        if row is not None and filed:
            known = await asyncio.to_thread(_journal_hashes, store, note_id)
            known.add(row.content_hash)
            if group.keeper is not None and group.keeper.content_hash:
                known.add(group.keeper.content_hash)
        remaining: list[Observation] = []
        linked: set[str] = set()
        for copy in group.copies:
            if await asyncio.to_thread(_through_symlink, store.notes_root, copy.path):
                linked.add(copy.path)
                remaining.append(copy)
                continue
            if (
                filed
                and copy.content_hash in known
                and await asyncio.to_thread(
                    _remove_verified,
                    store.notes_root,
                    copy.path,
                    review_folder,
                    copy.content_hash,
                    group.keeper_path,
                )
            ):
                log.info(
                    "removed review copy of a note already filed",
                    note_id=note_id,
                    removed=copy.path,
                    filed_at=group.keeper_path,
                )
                continue
            remaining.append(copy)
        if not remaining:
            continue
        unsettled += 1
        unresolved[note_id] = [group.keeper_path, *(item.path for item in remaining)]
        for copy in remaining:
            if copy.path in linked:
                reason = (
                    f"{copy.path} is a symbolic link that reads as a copy of this note. "
                    "It was not followed, changed or removed. Delete the link yourself."
                )
            elif filed and copy.content_hash in known:
                reason = (
                    f"{copy.path} is a copy of this note left in {copy.path.split('/')[0]} "
                    f"after it was filed at {group.keeper_path}, and could not be removed. "
                    "It holds nothing the filed note lacks. Delete it."
                )
            elif filed:
                reason = (
                    f"{copy.path} is a copy of this note left in {copy.path.split('/')[0]} "
                    f"after it was filed at {group.keeper_path}, and the two differ. "
                    "Neither was changed. Compare them and delete the one you do not want."
                )
            else:
                reason = (
                    f"{copy.path} carries the same note identity as {group.keeper_path}. "
                    "Neither was changed. Compare them and delete the one you do not want."
                )
            collisions.append((note_id, copy.path, "collision", reason))
    return unsettled


def _log_unresolved(store: LocalStore, unresolved: dict[str, list[str]]) -> None:
    """Name each collision once, and again only when the files involved change."""
    current = {note_id: tuple(sorted(set(paths))) for note_id, paths in unresolved.items()}
    previous: dict[str, tuple[str, ...]] = getattr(store, "_logged_collisions", {})
    for note_id, paths in current.items():
        if previous.get(note_id) != paths:
            log.warning(
                "duplicate note identity left unresolved",
                note_id=note_id,
                paths=list(paths),
            )
    store._logged_collisions = current


async def note_collisions(store: LocalStore, note_id: str) -> list[str]:
    """What the last pass recorded about other files carrying this note's identity."""
    try:
        async with store.session_factory() as session:
            return list(
                (
                    await session.scalars(
                        sa.select(RecordedRejection.reason)
                        .where(
                            RecordedRejection.kind == "collision",
                            RecordedRejection.reference == note_id,
                        )
                        .order_by(RecordedRejection.id)
                    )
                ).all()
            )
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc


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


def _walk(root: Path) -> Iterator[Path]:
    """Never interpret a silently skipped subtree as absence."""

    def failed(error: OSError) -> None:
        raise error

    for parent, directories, files in os.walk(root, onerror=failed, followlinks=False):
        directories[:] = [name for name in directories if name not in _IGNORED_DIRECTORIES]
        for name in directories + files:
            yield Path(parent) / name
