"""Preserved intake receipts and pluggable routing for the shared vault Dropbox."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from coppermind.atomicio import atomic_write_bytes
from coppermind.ids import new_id
from coppermind_store.calendar_import import MAX_UPLOAD
from coppermind_store.calendar_worker import isolated_calendar

if TYPE_CHECKING:
    from coppermind_store.context_service import ContextService


class Dropbox:
    def __init__(self, context: ContextService) -> None:
        self.context = context
        self.seen: dict[str, tuple[tuple[int, int], float]] = {}

    @staticmethod
    def handler(data: bytes, filename: str) -> str | None:
        """Extension point for future binary-source handlers; sniff actual content."""
        if data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"BEGIN:VCALENDAR"):
            return ".ics"
        if data.startswith(b"PK\x03\x04"):
            return ".zip"
        # Known extensions still reach validation so malformed input gets a useful error.
        suffix = Path(filename).suffix.lower()
        return suffix if suffix in {".ics", ".zip"} else None

    def records(self) -> dict[str, Any]:
        return self.context.read("dropbox", {"items": {}, "calendars": {}})

    def save(self, records: dict[str, Any]) -> None:
        self.context.write("dropbox", records)

    def submit(self, filename: str, data: bytes) -> dict[str, Any]:
        if not filename or len(filename) > 255 or any(ord(c) < 32 for c in filename):
            raise ValueError("invalid filename")
        if len(data) > MAX_UPLOAD:
            raise ValueError("Dropbox intake limit is 10 MiB; file remains in the vault")
        digest = hashlib.sha256(data).hexdigest()
        records = self.records()
        if digest in records["items"]:
            return records["items"][digest]
        atomic_write_bytes(self.context.root / "dropbox-originals" / digest, data, mode=0o600)
        records["items"][digest] = {
            "id": digest,
            "filename": filename,
            "state": "pending",
            "parts": [],
            "error": "",
        }
        self.save(records)
        return records["items"][digest]

    async def process(self) -> None:
        records = self.records()
        # Compact receipts written by older versions, including completed imports.
        archived = {r.id for r in self.context.archive.receipts()}
        compacted = False
        for item in records["items"].values():
            for part in item["parts"]:
                if part.get("import_id") in archived and "data" in part:
                    del part["data"]
                    compacted = True
        if compacted:
            self.save(records)
        for item in records["items"].values():
            if item["state"] != "pending":
                continue
            data = (self.context.root / "dropbox-originals" / item["id"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != item["id"]:
                raise ValueError("Dropbox original failed integrity verification")
            # Handlers inspect content as well as the declared suffix. Unsupported
            # originals are retained, never silently treated as completed imports.
            handler = self.handler(data, item["filename"])
            if handler is None:
                item["state"] = "unsupported"
                item["error"] = "Unsupported intake type; original preserved"
            else:
                try:
                    parts = await asyncio.to_thread(
                        isolated_calendar,
                        {
                            "action": "identify",
                            "filename": "calendar" + str(handler),
                            "data": base64.b64encode(data).decode(),
                        },
                    )
                    for part in parts:
                        uid = part["uid"]
                        bound = next(
                            (
                                c["id"]
                                for c in records["calendars"].values()
                                if uid and c.get("uid") == uid
                            ),
                            "",
                        )
                        if bound:
                            part["calendar_id"] = bound
                        elif uid and not self.context.config().sources:
                            identity = "cal_" + hashlib.sha256(uid.encode()).hexdigest()[:40]
                            records["calendars"].setdefault(
                                identity,
                                {
                                    "id": identity,
                                    "name": part["name"] or "Imported calendar",
                                    "uid": uid,
                                    "timezone": part["timezone"] or "America/Chicago",
                                },
                            )
                            part["calendar_id"] = identity
                        else:
                            part["calendar_id"] = ""
                    item["parts"] = parts
                    item["state"] = (
                        "identified" if all(p["calendar_id"] for p in parts) else "needs_identity"
                    )
                except ValueError as exc:
                    item["state"], item["error"] = "failed", str(exc)
            self.save(records)
            break
        for item in records["items"].values():
            if item["state"] not in {"identified", "importing"}:
                continue
            # All entries must be identified before submitting any of this archive.
            for part in item["parts"]:
                if not part.get("import_id"):
                    receipt = self.context.archive.submit(
                        part["calendar_id"], "calendar.ics", base64.b64decode(part["data"])
                    )
                    part["import_id"] = receipt.id
                    del part["data"]
                    # Persist each durable handoff so interrupted ZIP dispatch resumes
                    # without needing embedded bytes for already archived entries.
                    self.save(records)
            item["state"] = "importing"
            receipts = {r.id: r for r in self.context.archive.receipts()}
            if all(receipts[p["import_id"]].state == "succeeded" for p in item["parts"]):
                item["state"] = "succeeded"
            elif any(receipts[p["import_id"]].state == "failed" for p in item["parts"]):
                item["state"] = "failed"
                item["error"] = "Calendar import failed; inspect the calendar import receipt"
            self.save(records)

    async def identify(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.context.lock:
            records = self.records()
            item = records["items"].get(str(payload.get("id", "")))
            if not item or item["state"] != "needs_identity":
                raise ValueError("this import does not need calendar identification")
            index = int(payload["part"])
            if index < 0 or index >= len(item["parts"]):
                raise ValueError("invalid calendar selection")
            part = item["parts"][index]
            if part["calendar_id"]:
                raise ValueError("calendar already identified")
            target = str(payload.get("calendar_id", ""))
            if target:
                known = {s.id for s in self.context.config().sources} | set(records["calendars"])
                if target not in known:
                    raise ValueError("choose an existing calendar")
            else:
                name = str(payload.get("name", "")).strip()
                if not name or len(name) > 120:
                    raise ValueError("name this calendar")
                timezone = str(payload.get("timezone", "America/Chicago"))
                try:
                    ZoneInfo(timezone)
                except (ValueError, KeyError) as exc:
                    raise ValueError("choose a valid timezone") from exc
                target = "cal_" + new_id().lower()
                records["calendars"][target] = {
                    "id": target,
                    "name": name,
                    "uid": "",
                    "timezone": timezone,
                }
            if part["uid"]:
                existing = records["calendars"].get(target)
                if existing and existing.get("uid") not in {"", part["uid"]}:
                    raise ValueError("this calendar is already bound to another identifier")
                source = next((s for s in self.context.config().sources if s.id == target), None)
                if not existing:
                    if source is None:
                        raise ValueError("choose an existing calendar")
                    existing = {"name": source.name, "timezone": source.timezone}
                records["calendars"][target] = {
                    "id": target,
                    "name": existing["name"],
                    "timezone": existing["timezone"],
                    "uid": part["uid"],
                }
            part["calendar_id"] = target
            if all(p["calendar_id"] for p in item["parts"]):
                item["state"] = "identified"
            self.save(records)
            return {"identified": item["id"]}

    def snapshot(self) -> dict[str, Any]:
        records = self.records()
        return {
            "folder": self.context.config().dropbox_folder,
            "calendars": list(records["calendars"].values()),
            "items": [
                dict(
                    item, parts=[{k: v for k, v in p.items() if k != "data"} for p in item["parts"]]
                )
                for item in records["items"].values()
            ],
        }

    async def scan(self) -> None:
        root = self.context.store.notes_root / self.context.config().dropbox_folder
        if not root.exists():
            return
        if root.is_symlink() or any(p.is_symlink() for p in root.parents):
            raise ValueError("Dropbox must not be a symlink")
        legacy = [self.context.store.notes_root / s.folder for s in self.context.config().sources]
        if any(root == folder or folder in root.parents for folder in legacy):
            return
        count = 0
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [
                d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink()
            ]
            dirs[:] = [d for d in dirs if (Path(directory) / d) not in legacy]
            for name in files:
                count += 1
                if count > 1000:
                    raise ValueError("Dropbox exceeds 1000 files")
                path = Path(directory) / name
                if name.startswith(".") or path.suffix.lower() == ".md":
                    continue  # Markdown belongs to the ordinary note reconciler.
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                signature = (info.st_mtime_ns, info.st_size)
                old = self.seen.get(str(path))
                if old is None or old[0] != signature:
                    self.seen[str(path)] = (signature, time.monotonic())
                    continue
                if time.monotonic() - old[1] < 30:
                    continue
                if info.st_size > MAX_UPLOAD:
                    self.context.watch_status["dropbox"] = (
                        "A file exceeds 10 MiB; preserved in Dropbox"
                    )
                    continue
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    data = stream.read(MAX_UPLOAD + 1)
                    after = os.fstat(stream.fileno())
                if (before.st_mtime_ns, before.st_size) != signature or (
                    after.st_mtime_ns,
                    after.st_size,
                ) != signature:
                    continue
                async with self.context.lock:
                    self.submit(name, data)
