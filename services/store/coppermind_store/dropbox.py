"""Transient intake receipts backed by durable original exports and import history."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coppermind.atomicio import atomic_write_bytes
from coppermind_store.calendar_import import CALENDAR_ID, MAX_UPLOAD

if TYPE_CHECKING:
    from coppermind_store.context_service import ContextService

log = logging.getLogger(__name__)
COMPLETED = {"succeeded", "already_imported"}


class Dropbox:
    def __init__(self, context: ContextService) -> None:
        self.context = context
        self.seen: dict[str, tuple[tuple[int, int], float]] = {}

    @staticmethod
    def handler(data: bytes, filename: str) -> str | None:
        if data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"BEGIN:VCALENDAR"):
            return ".ics"
        if data.startswith(b"PK\x03\x04"):
            return ".zip"
        suffix = Path(filename).suffix.lower()
        return suffix if suffix in {".ics", ".zip"} else None

    def records(self) -> dict[str, Any]:
        records = self.context.read("dropbox", {"items": {}, "history": {}, "observations": {}})
        records.setdefault("history", {})
        records.setdefault("observations", {})
        return records

    def save(self, records: dict[str, Any]) -> None:
        self.context.write("dropbox", records)

    def expire(self, records: dict[str, Any]) -> None:
        cutoff = time.time() - self.context.config().dropbox_receipt_retention_s
        records["items"] = {
            key: item
            for key, item in records["items"].items()
            if item["state"] not in COMPLETED or item.get("completed_at", time.time()) > cutoff
        }

    def submit(self, filename: str, data: bytes) -> dict[str, Any]:
        if not filename or len(filename) > 255 or any(ord(c) < 32 for c in filename):
            raise ValueError("invalid filename")
        if len(data) > MAX_UPLOAD:
            raise ValueError("Dropbox intake limit is 200,000,000 bytes; file remains in the vault")
        digest = hashlib.sha256(data).hexdigest()
        records = self.records()
        existing = records["items"].get(digest)
        if existing and existing["state"] not in COMPLETED:
            return existing
        history = records["history"].get(digest, {})
        imported = next(
            (
                r
                for r in self.context.archive.receipts()
                if r.content_hash == digest and r.state == "succeeded"
            ),
            None,
        )
        if imported is not None:
            history = {"state": "succeeded", "parts": [{"import_id": imported.id}]}
        duplicate = history.get("state") in COMPLETED or (
            existing and existing["state"] in COMPLETED
        )
        atomic_write_bytes(self.context.root / "dropbox-originals" / digest, data, mode=0o600)
        item = {
            "id": digest,
            "filename": filename,
            "state": "already_imported" if duplicate else "pending",
            "parts": history.get("parts", []) if duplicate else [],
            "error": "",
            "received_at": datetime.now(UTC).isoformat(),
        }
        if duplicate:
            item["completed_at"] = time.time()
        records["items"][digest] = item
        self.save(records)
        log.info("Dropbox %s export %s", "skip" if duplicate else "adopt", digest)
        return item

    async def process(self) -> None:
        records = self.records()
        self.expire(records)
        for item in records["items"].values():
            if item["state"] == "pending":
                try:
                    data = (self.context.root / "dropbox-originals" / item["id"]).read_bytes()
                    if hashlib.sha256(data).hexdigest() != item["id"]:
                        raise ValueError("Dropbox original failed integrity verification")
                    handler = self.handler(data, item["filename"])
                    if handler is None:
                        item["state"] = "unsupported"
                        item["error"] = (
                            "Unsupported file type. Original preserved; dismiss this receipt."
                        )
                        log.info("Dropbox park export %s: unsupported file type", item["id"])
                    else:
                        name = item["filename"]
                        if Path(name).suffix.lower() != handler:
                            name += handler
                        receipt = await asyncio.to_thread(
                            self.context.archive.submit,
                            CALENDAR_ID,
                            name,
                            data,
                            export_filename=item["filename"],
                        )
                        item["parts"] = [{"import_id": receipt.id}]
                        item["state"] = "importing"
                except (ValueError, OSError) as exc:
                    item["state"], item["error"] = "failed", str(exc)
                    log.info("Dropbox fail export %s", item["id"])
                self.save(records)
                break
        receipts = {r.id: r for r in self.context.archive.receipts()}
        for item in records["items"].values():
            if item["state"] in {"importing", "failed"} and item["parts"]:
                imports = [receipts[p["import_id"]] for p in item["parts"]]
                if all(r.state == "succeeded" for r in imports):
                    item["state"] = "succeeded"
                    item["error"] = ""
                    item["completed_at"] = time.time()
                    log.info("Dropbox adopt completed export %s", item["id"])
                elif item["state"] == "importing" and any(r.state == "failed" for r in imports):
                    item["state"] = "failed"
                    item["error"] = next(r.error for r in imports if r.state == "failed")
                    log.info("Dropbox fail export %s", item["id"])
            if item["state"] not in {"pending", "importing"}:
                records["history"][item["id"]] = dict(item)
        self.save(records)

    async def dismiss(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.context.lock:
            records = self.records()
            identity = str(payload.get("id", ""))
            item = records["items"].get(identity)
            if not item or item["state"] not in COMPLETED | {"failed", "unsupported"}:
                raise ValueError("Only completed or failed receipts can be dismissed")
            if item["state"] == "failed":
                for part in item["parts"]:
                    self.context.archive.dismiss(part["import_id"])
            records["history"][identity] = item
            del records["items"][identity]
            self.save(records)
            return {"dismissed": identity}

    def snapshot(self) -> dict[str, Any]:
        records = self.records()
        self.expire(records)
        return {
            "folder": self.context.config().dropbox_folder,
            "items": [
                dict(
                    item,
                    parts=[
                        {k: v for k, v in part.items() if k != "data"} for part in item["parts"]
                    ],
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
        present = set()
        count = 0
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [
                d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink()
            ]
            for name in files:
                path = Path(directory) / name
                if name.startswith(".") or path.suffix.lower() == ".md":
                    continue
                count += 1
                if count > 1000:
                    raise ValueError("Dropbox exceeds 1000 files")
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                key = str(path.relative_to(root))
                present.add(key)
                signature = (info.st_mtime_ns, info.st_size)
                records = self.records()
                if records["observations"].get(key) == list(signature):
                    continue
                old = self.seen.get(key)
                if old is None or old[0] != signature:
                    self.seen[key] = (signature, time.monotonic())
                    log.info("Dropbox park file: waiting for stable bytes")
                    continue
                if time.monotonic() - old[1] < 30:
                    continue
                async with self.context.lock:
                    if info.st_size > MAX_UPLOAD:
                        identity = hashlib.sha256(f"{key}:{signature}".encode()).hexdigest()
                        records["items"][identity] = {
                            "id": identity,
                            "filename": name,
                            "state": "failed",
                            "parts": [],
                            "error": "File exceeds 200,000,000 bytes. Original remains in Dropbox.",
                        }
                        log.info("Dropbox fail file: intake byte limit exceeded")
                    else:
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
                        self.submit(name, data)
                        records = self.records()
                    records["observations"][key] = list(signature)
                    self.save(records)
        async with self.context.lock:
            records = self.records()
            records["observations"] = {
                k: v for k, v in records["observations"].items() if k in present
            }
            self.save(records)
