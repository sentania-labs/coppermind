"""Transient intake receipts backed by durable original exports and import history.

Calendar exports route themselves by their bytes. Anything else needs a kind
marker to be taken at all: a sidecar `<filename>.kind` beside the dropped file
(the word `artifact`, or JSON with `kind` and optional `title`, `tags` and
`date`), or `kind=artifact` on the upload route. Without one the file is
parked as unsupported rather than guessed at, because a guess is how a design
package ends up filed as a meeting.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import stat
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from coppermind.atomicio import atomic_write_bytes
from coppermind.store_protocol import CreateArtifact, CreatedArtifact, StoreError
from coppermind_store.calendar_import import CALENDAR_ID, MAX_UPLOAD

if TYPE_CHECKING:
    from coppermind_store.context_service import ContextService

log = logging.getLogger(__name__)
COMPLETED = {"succeeded", "already_imported"}
RECEIPT_RETENTION_S = 7 * 86400
KIND_SUFFIX = ".kind"
OFFICE_SUFFIXES = {".pptx", ".docx", ".xlsx", ".odt", ".odp", ".ods", ".epub"}
NO_KIND = (
    "No kind marker. Drop a sidecar named <filename>.kind containing the word artifact, "
    "or upload through POST /v1/artifacts with kind: artifact. Original preserved."
)


def read_kind_marker(data: bytes) -> dict[str, Any] | None:
    """What a `.kind` sidecar declares, or None when it declares nothing usable.

    Plain text `artifact` is enough. JSON may add `title`, `tags` and `date`,
    the same declared metadata the upload route takes.
    """
    try:
        text = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not text or len(text) > 4096:
        return None
    if text.startswith("{"):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(loaded, dict) or loaded.get("kind") != "artifact":
            return None
        marker: dict[str, Any] = {"kind": "artifact"}
        if isinstance(loaded.get("title"), str) and loaded["title"].strip():
            marker["title"] = loaded["title"].strip()
        if isinstance(loaded.get("tags"), list):
            marker["tags"] = [tag for tag in loaded["tags"] if isinstance(tag, str)]
        if isinstance(loaded.get("date"), str):
            marker["date"] = loaded["date"]
        return marker
    return {"kind": "artifact"} if text.split()[0].lower() == "artifact" else None


class Dropbox:
    def __init__(self, context: ContextService) -> None:
        self.context = context
        self.seen: dict[str, tuple[tuple[int, int], float]] = {}

    @staticmethod
    def handler(data: bytes, filename: str) -> str | None:
        """The calendar handler for these bytes, or None when they are not a calendar.

        Office documents are ZIP containers too, so a deck or a document named
        as one is never mistaken for a zipped calendar export.
        """
        suffix = Path(filename).suffix.lower()
        if suffix in OFFICE_SUFFIXES:
            return None
        if data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"BEGIN:VCALENDAR"):
            return ".ics"
        if data.startswith(b"PK\x03\x04"):
            return ".zip"
        return suffix if suffix in {".ics", ".zip"} else None

    def records(self) -> dict[str, Any]:
        records = self.context.read("dropbox", {"items": {}, "history": {}, "observations": {}})
        records.setdefault("history", {})
        records.setdefault("observations", {})
        return records

    def save(self, records: dict[str, Any]) -> None:
        self.context.write("dropbox", records)

    def expire(self, records: dict[str, Any]) -> None:
        cutoff = time.time() - RECEIPT_RETENTION_S
        records["items"] = {
            key: item
            for key, item in records["items"].items()
            if item["state"] not in COMPLETED or item.get("completed_at", time.time()) > cutoff
        }

    def submit(
        self, filename: str, data: bytes, kind: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not filename or len(filename) > 255 or any(ord(c) < 32 for c in filename):
            raise ValueError("invalid filename")
        if len(data) > MAX_UPLOAD:
            raise ValueError("Dropbox intake limit is 200 MB; file remains in the vault")
        if kind is not None and kind.get("kind") != "artifact":
            raise ValueError("kind: only artifact is accepted; calendars route themselves")
        digest = hashlib.sha256(data).hexdigest()
        records = self.records()
        existing = records["items"].get(digest)
        if existing and existing["state"] not in COMPLETED | {"failed"}:
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
            "kind": kind or {},
        }
        if duplicate:
            item["completed_at"] = time.time()
        records["items"][digest] = item
        self.save(records)
        log.info("Dropbox %s export %s", "skip" if duplicate else "adopt", digest)
        return item

    def original(self, digest: str) -> bytes:
        data = (self.context.root / "dropbox-originals" / digest).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Dropbox original failed integrity verification")
        return data

    @staticmethod
    def read_stable(path: Path, signature: tuple[int, int]) -> bytes | None:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            data = stream.read(MAX_UPLOAD + 1)
            after = os.fstat(stream.fileno())
        if (before.st_mtime_ns, before.st_size) != signature or (
            after.st_mtime_ns,
            after.st_size,
        ) != signature:
            return None
        return data

    async def process(self) -> None:
        records = self.records()
        self.expire(records)
        for item in records["items"].values():
            if item["state"] == "pending":
                try:
                    data = await asyncio.to_thread(self.original, item["id"])
                    handler = self.handler(data, item["filename"])
                    marker = item.get("kind") or {}
                    if marker.get("kind") == "artifact":
                        stored = await self._store_artifact(item, data, marker)
                        item["parts"] = [{"artifact_id": stored.id}]
                        item["state"] = "succeeded"
                        item["completed_at"] = time.time()
                        log.info("Dropbox adopt artifact %s", item["id"])
                    elif handler is None:
                        item["state"] = "unsupported"
                        item["error"] = NO_KIND
                        log.info("Dropbox park export %s: no kind marker", item["id"])
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
                except (ValueError, OSError, StoreError) as exc:
                    item["state"], item["error"] = "failed", str(exc)
                    log.info("Dropbox fail export %s", item["id"])
                self.save(records)
                break
        receipts = {r.id: r for r in await asyncio.to_thread(self.context.archive.receipts)}
        for item in records["items"].values():
            if (
                item["state"] in {"importing", "failed"}
                and item["parts"]
                and all("import_id" in part for part in item["parts"])
            ):
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

    async def _store_artifact(
        self, item: dict[str, Any], data: bytes, marker: dict[str, Any]
    ) -> CreatedArtifact:
        """Hand a marked drop to the artifact store, declared kind and all."""
        received: date | None = None
        if isinstance(marker.get("date"), str):
            try:
                received = date.fromisoformat(marker["date"])
            except ValueError:
                raise ValueError("kind marker: date must be YYYY-MM-DD") from None
        try:
            request = CreateArtifact(
                kind="artifact",
                title=marker.get("title"),
                date=received,
                tags=list(marker.get("tags", [])),
                origin="dropbox",
                filename=item["filename"],
                content_base64=base64.b64encode(data).decode(),
            )
        except ValidationError as exc:
            raise ValueError("; ".join(error["msg"] for error in exc.errors())) from exc
        return await self.context.store.create_artifact(request)

    async def dismiss(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.context.lock:
            records = self.records()
            identity = str(payload.get("id", ""))
            item = records["items"].get(identity)
            if not item or item["state"] not in COMPLETED | {"failed", "unsupported"}:
                raise ValueError("Only completed or failed receipts can be dismissed")
            if item["state"] == "failed":
                for part in item["parts"]:
                    await asyncio.to_thread(self.context.archive.dismiss, part["import_id"])
            records["history"][identity] = item
            del records["items"][identity]
            self.save(records)
            return {"dismissed": identity}

    def snapshot(self) -> dict[str, Any]:
        records = self.records()
        self.expire(records)
        receipts = {r.id: r for r in self.context.archive.receipts()}
        items = []
        for item in records["items"].values():
            imports = [
                receipts[p["import_id"]] for p in item["parts"] if p.get("import_id") in receipts
            ]
            items.append(
                dict(
                    item,
                    event_count=sum(r.event_count for r in imports),
                    retryable=any(r.retryable for r in imports if r.state == "failed"),
                    first_imported_at=min(
                        (r.imported_at or r.received_at for r in imports if r.state == "succeeded"),
                        default="",
                    ),
                    parts=[
                        {k: v for k, v in part.items() if k != "data"} for part in item["parts"]
                    ],
                )
            )
        items.extend(
            {"id": "", "filename": key, "state": "stabilizing", "parts": [], "error": ""}
            for key, (signature, _) in self.seen.items()
            if records["observations"].get(key) != list(signature)
        )
        return {
            "folder": self.context.config().dropbox_folder,
            "items": items,
        }

    async def scan(self) -> None:
        root = self.context.store.notes_root / self.context.config().dropbox_folder
        if not root.exists():
            self.seen.clear()
            return
        if root.is_symlink() or any(p.is_symlink() for p in root.parents):
            raise ValueError("Dropbox must not be a symlink")
        present = set()
        count = 0
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [
                d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink()
            ]
            markers = {name for name in files if name.endswith(KIND_SUFFIX)}
            for name in files:
                path = Path(directory) / name
                if name.startswith(".") or path.suffix.lower() == ".md" or name in markers:
                    continue
                count += 1
                if count > 1000:
                    if count == 1001:
                        log.info("Dropbox fail folder: more than 1000 files; extras not scanned")
                    continue
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
                    records = self.records()
                    error = ""
                    if info.st_size > MAX_UPLOAD:
                        error = "File exceeds 200 MB. Original remains in Dropbox."
                    else:
                        try:
                            data = await asyncio.to_thread(self.read_stable, path, signature)
                            if data is None:
                                continue
                            marker = None
                            if name + KIND_SUFFIX in markers:
                                marker = read_kind_marker(
                                    (path.parent / (name + KIND_SUFFIX)).read_bytes()
                                )
                                if marker is None:
                                    raise ValueError(
                                        "The .kind sidecar does not declare artifact; "
                                        "nothing was stored."
                                    )
                            await asyncio.to_thread(self.submit, name, data, marker)
                            records = self.records()
                        except ValueError as exc:
                            error = str(exc)
                        except OSError as exc:
                            error = f"File could not be read: {exc.strerror or 'I/O error'}"
                    if error:
                        identity = hashlib.sha256(f"{key}:{signature}".encode()).hexdigest()
                        records["items"][identity] = {
                            "id": identity,
                            "filename": name,
                            "state": "failed",
                            "parts": [],
                            "error": error,
                        }
                        log.info("Dropbox fail file: intake rejected")
                    records["observations"][key] = list(signature)
                    self.save(records)
        async with self.context.lock:
            self.seen = {key: value for key, value in self.seen.items() if key in present}
            records = self.records()
            records["observations"] = {
                k: v for k, v in records["observations"].items() if k in present
            }
            self.save(records)
