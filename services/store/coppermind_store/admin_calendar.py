"""The calendar as Admin shows it: files, meetings, and disagreements between files.

Counts are worked out once from the meetings themselves and remembered until the
calendar changes, so the Calendar page, Recordings and imports, and Home all read
the same numbers.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from coppermind.db.calendar import calendar_events as rows
from coppermind_store import calendar_mirror as mirror
from coppermind_store.calendar_import import (
    CALENDAR_ID,
    CalendarEvent,
    CalendarImportError,
    _differences,
)
from coppermind_store.calendar_worker import parse_bounded

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore

# One summary of every meeting, kept until the catalog's signature changes.
_CACHE: dict[str, tuple[str, dict[str, Any]]] = {}


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
    with archive.engine.connect() as connection:
        signature = mirror.get_state(connection, "signature")
        cached = _CACHE.get(key)
        if cached and cached[0] == signature:
            return cached[1]
        events = []
        files: dict[str, dict[str, int]] = {}
        records = {
            (c.identity, c.conflict_version or c.semantic_hash)
            for c in mirror.get_conflicts(connection)
        }
        held: dict[str, set[str]] = {}
        for receipt in archive.receipts():
            for problem in receipt.conflicts:
                if (problem.event_id, problem.version) in records:
                    held.setdefault(problem.event_id, set()).add(receipt.id)
        for row in connection.execute(sa.select(rows.c.id, rows.c.record)):
            record = json.loads(row.record)
            provenance = record.get("provenance") or []
            sources = list(dict.fromkeys(item["source_id"] for item in provenance))
            for source in sources:
                entry = files.setdefault(source, {"has": 0, "adds": 0, "only": 0})
                entry["has"] += 1
            if sources:
                first = min(provenance, key=lambda item: item["imported_at"])["source_id"]
                files[first]["adds"] += 1
                if len(sources) == 1 and not held.get(row.id, set()) - {sources[0]}:
                    files[sources[0]]["only"] += 1
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
            entry = counts.get(receipt.id, {"has": 0, "adds": 0, "only": 0})
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

    def _incoming(
        self, event_id: str, version: str, exclude: str = ""
    ) -> tuple[CalendarEvent, Any] | None:
        """The other version of a disagreement, read back from the file that carried it."""
        cache = self.archive.root / "conflict-incoming" / f"{event_id}-{version}.json"
        receipts = {r.id: r for r in self.archive.receipts() if r.id != exclude}
        if cache.exists():
            saved = json.loads(cache.read_bytes())
            receipt = receipts.get(saved["receipt"])
            if receipt is not None:
                return CalendarEvent.model_validate(saved["event"]), receipt
        for receipt in sorted(receipts.values(), key=lambda r: r.received_at, reverse=True):
            if not any(p.event_id == event_id and p.version == version for p in receipt.conflicts):
                continue
            original = self.archive.root / "imports" / receipt.id / "original"
            try:
                limits = self.archive.limits()
                events = parse_bounded(
                    CALENDAR_ID,
                    receipt.filename,
                    original.read_bytes(),
                    receipt.limit,
                    max_events=limits.calendar_max_events,
                    memory_mib=limits.calendar_import_memory_mib,
                )
            except (OSError, CalendarImportError):
                continue
            for event in events:
                if event.identity == event_id and event.semantic_hash == version:
                    from coppermind_store.calendar_import import EventProvenance

                    event.provenance = [
                        EventProvenance(
                            source_id=receipt.id,
                            filename=receipt.export_filename or receipt.filename,
                            entry=receipt.original_entry or event.source_entry,
                            imported_at=receipt.received_at,
                        )
                    ]
                    from coppermind.atomicio import atomic_write_text

                    atomic_write_text(
                        cache,
                        json.dumps({"receipt": receipt.id, "event": event.model_dump(mode="json")}),
                        mode=0o600,
                    )
                    return event, receipt
        return None

    def _conflicts(self) -> list[dict[str, Any]]:
        catalog = self.archive.catalog()
        names = {
            receipt.id: (receipt.export_filename or receipt.filename, receipt.received_at)
            for receipt in self.archive.receipts()
        }
        out = []
        for retained in catalog.conflicts:
            version = retained.conflict_version or retained.semantic_hash
            try:
                stored = catalog.events[retained.identity]
            except KeyError:
                continue
            other = self._incoming(retained.identity, version)
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

    async def attention(self) -> dict[str, Any]:
        """What the calendar needs from its owner: disagreements and files that failed."""
        return await asyncio.to_thread(
            lambda: {"conflicts": self._conflicts(), "bad_files": self._bad_files()}
        )

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
                now = datetime.now(UTC).isoformat()
                past = [event for event in events if event["start"][:19] <= now[:19]]
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

        return await asyncio.to_thread(read)

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

    async def add(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add a calendar file and read it at once, so the page can say what it held."""
        context = self.store.context
        data = base64.b64decode(str(payload["content_base64"]), validate=True)
        filename = str(payload["filename"])
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
                found = self._incoming(event_id, version)
                if found is None:
                    raise ValueError(
                        "The other version can no longer be read from its file. "
                        "Add that calendar file again."
                    )
                self.archive.choose_incoming(event_id, version, found[0])
                chosen = conflict["incoming"]
            return {"title": conflict["title"], "start": chosen["start"], "file": chosen["file"]}

        return await asyncio.to_thread(apply)

    async def remove_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Remove one calendar file and the meetings that came only from it."""
        identity = str(payload["id"])
        context = self.store.context
        receipt = next((r for r in self.archive.receipts() if r.id == identity), None)
        if receipt is None:
            raise ValueError("That calendar file is no longer listed.")

        def other_file_version(event_id: str, version: str) -> CalendarEvent | None:
            found = self._incoming(event_id, version, exclude=identity)
            return found[0] if found else None

        counts = await asyncio.to_thread(self.archive.remove_import, identity, other_file_version)
        async with context.lock:
            records = context.dropbox.records()
            for group in ("items", "history"):
                for key, item in list(records[group].items()):
                    if any(part.get("import_id") == identity for part in item.get("parts", [])):
                        del records[group][key]
            context.dropbox.save(records)
        summary = await asyncio.to_thread(_summary, self.archive)
        return {
            "name": receipt.export_filename or receipt.filename,
            "removed": counts["removed"],
            "total": len(summary["events"]),
            "files": len(await asyncio.to_thread(self._files)),
        }

    async def set_max(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = int(payload["max_events"])
        if value not in (10_000, 50_000, 200_000):
            raise ValueError("choose one of the listed sizes")
        state = self.store.control.store.read("settings")
        body = dict(state.body)
        body.setdefault("limits", {})["calendar_max_events"] = value
        self.store.control.store.write("settings", body, if_revision=state.revision)
        return {"max_events": value}
