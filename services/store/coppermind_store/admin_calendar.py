"""The calendar as Admin shows it: files, meetings, and disagreements between files.

Counts are worked out once from the meetings themselves and remembered until the
calendar changes, so the Calendar page and Recordings and imports read the same
numbers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from coppermind.db.calendar import calendar_events as rows
from coppermind_store import calendar_mirror as mirror
from coppermind_store.calendar_archive import incoming_path, save_incoming
from coppermind_store.calendar_import import (
    CALENDAR_ID,
    CalendarEvent,
    CalendarImportError,
    _differences,
)
from coppermind_store.calendar_worker import parse_bounded

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore

log = logging.getLogger(__name__)

CALENDAR_UPLOAD_LIMIT = 200_000_000

# How long a page waits for the calendar's part before it is shown without it. Well inside
# the 30 seconds Admin and the companion wait for the store, so a slow calendar costs the
# page one section, never the whole page.
CALENDAR_BUDGET_SECONDS = 8.0

# One summary of every meeting, kept until the catalog's signature changes.
_CACHE: dict[str, tuple[str, dict[str, Any]]] = {}
# One reader at a time per calendar: a request that finds the summary stale waits for the
# one already rebuilding it instead of reading every meeting again beside it.
_BUILDING: dict[str, threading.Lock] = {}
# Disagreements whose other version is being read back from its file, and ones already tried.
_FILLING: dict[str, asyncio.Task[None]] = {}
_TRIED: set[tuple[str, str, str]] = set()


def _distinct(people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each person once. The organizer of a meeting is usually listed as invited as well."""
    seen: dict[str, dict[str, Any]] = {}
    for person in people:
        key = str(person.get("address") or person.get("name") or "").strip().lower()
        if key:
            seen.setdefault(key, person)
    return list(seen.values())


def _summary(archive: Any) -> dict[str, Any]:
    """Every meeting in brief, and what each file contributed, read once per change."""
    key = str(archive.root)
    with _BUILDING.setdefault(key, threading.Lock()):
        return _build_summary(archive, key)


def _build_summary(archive: Any, key: str) -> dict[str, Any]:
    with archive.engine.connect() as connection:
        signature = mirror.get_state(connection, "signature")
        cached = _CACHE.get(key)
        if cached and cached[0] == signature:
            return cached[1]
        events = []
        files: dict[str, dict[str, int]] = {}
        for row in connection.execute(sa.select(rows.c.id, rows.c.record)):
            record = json.loads(row.record)
            provenance = record.get("provenance") or []
            sources = list(dict.fromkeys(item["source_id"] for item in provenance))
            for source in sources:
                entry = files.setdefault(source, {"has": 0, "adds": 0})
                entry["has"] += 1
            if sources:
                first = min(provenance, key=lambda item: item["imported_at"])["source_id"]
                files[first]["adds"] += 1
            events.append(
                {
                    "id": row.id,
                    "title": record.get("title") or "Untitled meeting",
                    "start": record.get("start") or "",
                    "end": record.get("end") or "",
                    "invited": len(_distinct(record.get("people") or [])),
                    "cancelled": bool(record.get("cancelled")),
                    "sources": sources,
                }
            )
    events.sort(key=lambda item: item["start"], reverse=True)
    summary = {"events": events, "files": files}
    _CACHE[key] = (signature, summary)
    return summary


def file_counts(archive: Any) -> dict[str, dict[str, int]]:
    return _summary(archive)["files"]


def _started(start: str, now: datetime) -> bool:
    try:
        moment = datetime.fromisoformat(start)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment <= now


class AdminCalendar:
    def __init__(self, store: LocalStore) -> None:
        self.store = store

    @property
    def archive(self) -> Any:
        return self.store.context.archive

    def _files(self) -> list[dict[str, Any]]:
        counts = file_counts(self.archive)
        files = []
        for receipt in sorted(self.archive.receipts(), key=lambda r: r.received_at, reverse=True):
            if receipt.state != "succeeded":
                continue
            entry = counts.get(receipt.id, {"has": 0, "adds": 0})
            files.append(
                {
                    "id": receipt.id,
                    "name": receipt.export_filename or receipt.filename,
                    "added": receipt.received_at,
                    **entry,
                    # What the file held, counting a meeting another file disagrees about.
                    "has": receipt.event_count + receipt.kept_count or entry["has"],
                }
            )
        return files

    def _bad_files(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "c:" + receipt.id,
                "name": receipt.export_filename or receipt.filename,
                "why": receipt.error,
                "added": receipt.received_at,
            }
            for receipt in self.archive.receipts()
            if receipt.state == "failed" and not receipt.dismissed
        ]

    def _saved_incoming(
        self, event_id: str, version: str, receipts: dict[str, Any]
    ) -> tuple[CalendarEvent, Any] | None:
        """The other version of a disagreement, if it was kept when its file was imported."""
        try:
            saved = json.loads(incoming_path(self.archive.root, event_id, version).read_bytes())
            receipt = receipts.get(saved["receipt"])
            if receipt is not None:
                return CalendarEvent.model_validate(saved["event"]), receipt
        except (OSError, ValueError, KeyError):
            pass
        return None

    def _parse_original(self, receipt: Any) -> list[CalendarEvent] | None:
        original = self.archive.root / "imports" / receipt.id / "original"
        try:
            limits = self.archive.limits()
            return parse_bounded(
                CALENDAR_ID,
                receipt.filename,
                original.read_bytes(),
                receipt.limit,
                max_events=limits.calendar_max_events,
                memory_mib=limits.calendar_import_memory_mib,
            )
        except (OSError, CalendarImportError):
            return None

    def _incoming(
        self, event_id: str, version: str, receipts: dict[str, Any] | None = None
    ) -> tuple[CalendarEvent, Any] | None:
        """The other version of a disagreement, read back from the file that carried it.

        This reads and parses the calendar file when no copy was kept, so only a person's own
        choice calls it. Pages read `_saved_incoming` and fill the rest in the background.
        """
        receipts = receipts or {receipt.id: receipt for receipt in self.archive.receipts()}
        found = self._saved_incoming(event_id, version, receipts)
        if found is not None:
            return found
        for receipt in sorted(receipts.values(), key=lambda r: r.received_at, reverse=True):
            if not any(p.event_id == event_id and p.version == version for p in receipt.conflicts):
                continue
            events = self._parse_original(receipt)
            for event in events or []:
                if event.identity == event_id and event.semantic_hash == version:
                    save_incoming(self.archive.root, receipt, event)
                    return self._saved_incoming(event_id, version, receipts)
        return None

    def _fill_incoming(self) -> None:
        """Keep the other version of every disagreement that has none, one file read per file.

        Disagreements found before copies were kept at import have nothing saved. Each calendar
        file that carries one is read once, however many disagreements it holds, and what it
        held is kept so no page reads it again.
        """
        receipts = sorted(self.archive.receipts(), key=lambda r: r.received_at, reverse=True)
        root = str(self.archive.root)
        missing = {
            key
            for event in self.archive.catalog().conflicts
            if (key := (event.identity, event.conflict_version or event.semantic_hash))
            and not incoming_path(self.archive.root, *key).exists()
            and (root, *key) not in _TRIED
        }
        _TRIED.update((root, *key) for key in missing)
        for receipt in receipts:
            carried = {
                key
                for key in missing
                if any(p.event_id == key[0] and p.version == key[1] for p in receipt.conflicts)
            }
            if not carried:
                continue
            for event in self._parse_original(receipt) or []:
                if (event.identity, event.semantic_hash) in carried:
                    carried.discard((event.identity, event.semantic_hash))
                    missing.discard((event.identity, event.semantic_hash))
                    try:
                        save_incoming(self.archive.root, receipt, event)
                    except OSError:
                        log.warning("conflict_incoming_not_saved", exc_info=True)

    def _fill_missing(self, conflicts: list[dict[str, Any]]) -> None:
        """Start filling in what a page could not show yet, unless that is already under way."""
        key = str(self.archive.root)
        if not any(
            c["incoming"] is None and (key, c["id"], c["version"]) not in _TRIED for c in conflicts
        ):
            return
        running = _FILLING.get(key)
        if running is not None and not running.done():
            return
        task = asyncio.get_running_loop().create_task(asyncio.to_thread(self._fill_incoming))

        def finished(done: asyncio.Task[None]) -> None:
            _FILLING.pop(key, None)
            if not done.cancelled() and done.exception() is not None:
                log.warning("conflict_incoming_fill_failed", exc_info=done.exception())

        task.add_done_callback(finished)
        _FILLING[key] = task

    def _conflicts(self) -> list[dict[str, Any]]:
        """Every disagreement, with the other version when it was kept. Never reads a file."""
        catalog = self.archive.catalog()
        receipts = {receipt.id: receipt for receipt in self.archive.receipts()}
        names = {
            receipt.id: (receipt.export_filename or receipt.filename, receipt.received_at)
            for receipt in receipts.values()
        }
        out = []
        for retained in catalog.conflicts:
            version = retained.conflict_version or retained.semantic_hash
            try:
                stored = catalog.events[retained.identity]
            except KeyError:
                continue
            other = self._saved_incoming(retained.identity, version, receipts)
            first = stored.provenance[0].source_id if stored.provenance else ""
            entry: dict[str, Any] = {
                "id": retained.identity,
                "version": version,
                "title": stored.title or "Untitled meeting",
                "stored": {
                    "title": stored.title,
                    "start": stored.start,
                    "end": stored.end,
                    "file": names.get(first, ("an earlier calendar file", ""))[0],
                    "added": names.get(first, ("", ""))[1],
                },
                "incoming": None,
                "differs": "",
            }
            if other is not None:
                event, receipt = other
                entry["incoming"] = {
                    "title": event.title,
                    "start": event.start,
                    "end": event.end,
                    "file": receipt.export_filename or receipt.filename,
                    "added": receipt.received_at,
                }
                entry["differs"] = _differences(event, stored)
            out.append(entry)
        return out

    async def _degraded(self, call: Any, fallback: Any) -> Any:
        """Run one calendar read in a thread; a slow or failed one gives `fallback` instead."""
        try:
            return await asyncio.wait_for(asyncio.to_thread(call), CALENDAR_BUDGET_SECONDS)
        except Exception:  # noqa: BLE001 - the page is shown without its calendar piece
            log.warning("calendar_unavailable", exc_info=True)
            return fallback

    async def attention(self) -> dict[str, Any]:
        """What the calendar needs from its owner: disagreements and files that failed.

        A calendar that is slow or broken leaves this part out and says so; it never holds
        the rest of the page.
        """
        result = await self._degraded(
            lambda: {"conflicts": self._conflicts(), "bad_files": self._bad_files()},
            {"conflicts": [], "bad_files": [], "unavailable": True},
        )
        self._fill_missing(result["conflicts"])
        return result

    async def files(self) -> list[dict[str, Any]] | None:
        """The calendar files and what each held, or None when they cannot be read in time."""
        return await self._degraded(self._files, None)

    async def view(self, payload: dict[str, Any]) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            summary = _summary(self.archive)
            query = str(payload.get("q") or "").strip().casefold()
            events = [
                event
                for event in summary["events"]
                if not event["cancelled"] and (not query or query in event["title"].casefold())
            ]
            if not query:
                # Recent meetings: the newest that have already started, then what is next.
                now = datetime.now(UTC)
                past = [event for event in events if _started(event["start"], now)]
                events = past or events
            return {
                "files": self._files(),
                "total": len(summary["events"]),
                "events": events[:25],
                "matches": len(events),
                "conflicts": self._conflicts(),
                "bad_files": self._bad_files(),
                "max_events": self.store.control.settings().limits.calendar_max_events,
            }

        result = await asyncio.to_thread(read)
        self._fill_missing(result["conflicts"])
        return result

    async def meeting(self, payload: dict[str, Any]) -> dict[str, Any]:
        identity = str(payload["id"])

        def read() -> dict[str, Any]:
            try:
                event = self.archive.catalog().events[identity]
            except KeyError as exc:
                raise ValueError("That meeting is no longer in your calendar.") from exc
            return {
                "id": identity,
                "title": event.title or "Untitled meeting",
                "start": event.start,
                "end": event.end,
                "cancelled": event.cancelled,
                "invited": _distinct(
                    [
                        {"name": person.name, "address": person.address, "role": person.role}
                        for person in event.people
                    ]
                ),
                "files": list(dict.fromkeys(p.filename for p in event.provenance)),
            }

        result = await asyncio.to_thread(read)
        matched = []
        for note_id, association in self.store.context.read("associations", {}).items():
            if identity in association.get("events", []):
                try:
                    note = await self.store.get_note(note_id)
                except Exception:  # noqa: BLE001 - a note that has gone is simply not listed
                    continue
                matched.append({"id": note_id, "title": note.title})
        result["notes"] = matched
        result["recordings"] = [
            {"id": "r:" + source, "event": choice.get("event_id")}
            for source, choice in self.store.context.read("recording-meetings", {}).items()
            if choice.get("event_id") == identity
        ]
        return result

    async def add(self, filename: str, data: bytes) -> dict[str, Any]:
        """Add a calendar file and read it at once, so the page can say what it held."""
        context = self.store.context
        before = {receipt.id for receipt in self.archive.receipts()}
        async with context.lock:
            item = await asyncio.to_thread(context.dropbox.submit, filename, data, None)
        if item["state"] == "already_imported":
            return {"state": "already", "name": filename}
        for _ in range(4):
            async with context.lock:
                await context.dropbox.process()
            for receipt in self.archive.receipts():
                if receipt.state == "pending":
                    await asyncio.to_thread(self.archive.process, receipt.id)
        async with context.lock:
            await context.dropbox.process()
            current = context.dropbox.records()
        latest = current["items"].get(item["id"]) or current["history"].get(item["id"]) or item
        if latest["state"] == "unsupported":
            return {"state": "not_calendar", "name": filename}
        receipts = [
            receipt
            for receipt in self.archive.receipts()
            if receipt.id not in before
            or any(part.get("import_id") == receipt.id for part in latest.get("parts", []))
        ]
        failed = next((receipt for receipt in receipts if receipt.state == "failed"), None)
        if failed is not None or latest["state"] == "failed":
            return {
                "state": "failed",
                "name": filename,
                "why": failed.error if failed else latest.get("error", ""),
            }
        counts = file_counts(self.archive)
        ids = {receipt.id for receipt in receipts}
        held = [e for e in _summary(self.archive)["events"] if ids & set(e["sources"])]
        starts = sorted(e["start"] for e in held if e["start"])
        return {
            "state": "added",
            "name": filename,
            # What the file itself held, which counts a meeting another file disagrees about.
            "has": sum(r.event_count + r.kept_count for r in receipts)
            or sum(counts.get(i, {}).get("has", 0) for i in ids),
            "new": sum(counts.get(i, {}).get("adds", 0) for i in ids),
            "from": starts[0] if starts else "",
            "to": starts[-1] if starts else "",
            "total": len(_summary(self.archive)["events"]),
        }

    async def choose(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Settle a disagreement: keep the stored version, or use the other file's."""
        event_id, version = str(payload["id"]), str(payload["version"])
        pick = str(payload["pick"])
        if pick not in {"stored", "incoming"}:
            raise ValueError("Choose which version to use.")

        def apply() -> dict[str, Any]:
            conflict = next(
                (c for c in self._conflicts() if c["id"] == event_id and c["version"] == version),
                None,
            )
            if conflict is None:
                raise ValueError("That disagreement has already been settled.")
            if pick == "stored":
                self.archive.dismiss_conflict(event_id, version)
                chosen = conflict["stored"]
            else:
                # The one place a calendar file is read for a disagreement, and only the one
                # the owner chose.
                found = self._incoming(event_id, version)
                if found is None:
                    raise ValueError(
                        "The other version can no longer be read from its file. "
                        "Add that calendar file again."
                    )
                self.archive.choose_incoming(event_id, version, found[0])
                chosen = {
                    "start": found[0].start,
                    "file": found[1].export_filename or found[1].filename,
                }
            return {"title": conflict["title"], "start": chosen["start"], "file": chosen["file"]}

        return await asyncio.to_thread(apply)

    async def set_max(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = int(payload["max_events"])
        if value not in (10_000, 50_000, 200_000):
            raise ValueError("choose one of the listed sizes")
        state = self.store.control.store.read("settings")
        body = dict(state.body)
        body.setdefault("limits", {})["calendar_max_events"] = value
        self.store.control.store.write("settings", body, if_revision=state.revision)
        return {"max_events": value}
