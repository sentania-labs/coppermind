"""Transient intake receipts backed by durable original exports and import history.

Calendar exports route by their bytes; known artifact formats go to the artifact
store. Optional kind markers provide artifact metadata.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import logging
import os
import stat
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from coppermind.atomicio import atomic_write_bytes, create_exclusive_bytes, sync_directory
from coppermind.store_protocol import CreateArtifact, CreatedArtifact, StoreError
from coppermind_store.calendar_import import CALENDAR_ID, MAX_UPLOAD
from coppermind_store.extraction import EXTENSION_TYPES

if TYPE_CHECKING:
    from coppermind_store.context_service import ContextService

log = logging.getLogger(__name__)
COMPLETED = {"succeeded", "already_imported"}
RECEIPT_RETENTION_S = 7 * 86400
KIND_SUFFIX = ".kind"
OFFICE_SUFFIXES = {
    ".ppt",
    ".pptx",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".odt",
    ".odp",
    ".ods",
    ".epub",
    ".rtf",
}
ARTIFACT_SUFFIXES = EXTENSION_TYPES.keys() | OFFICE_SUFFIXES
NO_KIND = "This file type is not supported. Choose a supported format and try again."
NEEDS_KIND = "This document needs kind=artifact on the upload to be stored as an artifact."


def reason_name(name: str) -> str:
    return name.encode()[:200].decode(errors="ignore") + ".reason.txt"


def no_kind_message(filename: str) -> str:
    return NEEDS_KIND if Path(filename).suffix.lower() in ARTIFACT_SUFFIXES else NO_KIND


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
        self.seen: dict[str, tuple[tuple[int, int, int, int], float]] = {}

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
        records.setdefault("paths", {})
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
        if existing and existing["state"] not in COMPLETED | {"failed", "unsupported"}:
            return existing
        history: dict[str, Any] = {}
        duplicate = False
        if kind is None:
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
            duplicate = history.get("state") in COMPLETED or bool(
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
        if existing and existing.get("failed_path"):
            item["failed_path"] = existing["failed_path"]
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

    @staticmethod
    def sidecar_signature(path: Path) -> tuple[int, int]:
        try:
            info = path.lstat()
        except OSError:
            return (0, -1)
        return (info.st_mtime_ns, info.st_size)

    async def process(self) -> None:
        records = self.records()
        before = copy.deepcopy(records)
        self.expire(records)
        for item in records["items"].values():
            if item["state"] == "pending":
                try:
                    data = await asyncio.to_thread(self.original, item["id"])
                    handler = self.handler(data, item["filename"])
                    marker = item.get("kind") or {}
                    if marker.get("kind") == "artifact":
                        stored = await self._store_artifact(item, data, marker)
                        item["parts"] = [{"artifact_id": stored.id, "path": stored.path}]
                        item["state"] = "succeeded"
                        item["completed_at"] = time.time()
                        log.info("Dropbox adopt artifact %s", item["id"])
                    elif handler is None:
                        item["state"] = "unsupported"
                        item["error"] = no_kind_message(item["filename"])
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
        if records != before:
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

    @staticmethod
    def display_error(item: dict[str, Any]) -> str:
        error = item.get("error", "")
        if item.get("state") == "unsupported" and error.startswith("No kind marker."):
            error = no_kind_message(item["filename"])
        if item.get("state") in {"failed", "unsupported"} and item.get("original_path"):
            if item.get("failed_path"):
                error += (
                    " Find the file in Dropbox/Failed, fix it and move it back to Dropbox to retry."
                )
            else:
                error += (
                    " The file is still in Dropbox while Coppermind tries to move it to Failed."
                )
        return error

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
                    error=self.display_error(item),
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
            for key in self.waiting(records)
        )
        return {
            "folder": self.context.config().dropbox_folder,
            "items": items,
        }

    def waiting(self, records: dict[str, Any]) -> list[str]:
        root = self.context.store.notes_root / self.context.config().dropbox_folder
        if not root.is_dir() or root.is_symlink() or any(p.is_symlink() for p in root.parents):
            return []
        waiting = []
        for path, _ in self.candidates(root):
            try:
                info = path.lstat()
            except OSError:
                continue
            key = str(path.relative_to(root))
            if stat.S_ISREG(info.st_mode) and not (
                key in records["paths"]
                and records["observations"].get(key) == list(self.observe(info, path))
            ):
                waiting.append(key)
        return waiting

    @classmethod
    def observe(cls, info: os.stat_result, path: Path) -> tuple[int, int, int, int]:
        return (info.st_mtime_ns, info.st_size) + cls.sidecar_signature(
            path.with_name(path.name + KIND_SUFFIX)
        )

    @staticmethod
    def candidates(root: Path) -> Iterator[tuple[Path, set[str]]]:
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".")
                and not (Path(directory) / d).is_symlink()
                and (Path(directory) / d) != root / "Failed"
            ]
            markers = {name for name in files if name.endswith(KIND_SUFFIX)}
            for name in files:
                path = Path(directory) / name
                if name.startswith(".") or path.suffix.lower() == ".md" or name in markers:
                    continue
                yield path, markers

    def clear_failed(self, root: Path, item: dict[str, Any]) -> bool:
        """Drop the Failed copy of a drop that later succeeded, if the stored bytes match it."""
        target = root / item["failed_path"]
        failed = root / "Failed"
        try:
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode):
                return True
            data = self.read_stable(target, (info.st_mtime_ns, info.st_size))
            if data is None:
                return False
            reason = target.with_name(reason_name(target.name))
            if hashlib.sha256(data).hexdigest() != item["id"]:
                reason.write_text(
                    "This import later succeeded, but this copy differs from the stored one "
                    "and was left here.\n"
                )
                return True
            target.unlink()
            reason.unlink(missing_ok=True)
            if target.parent != failed:
                target.parent.rmdir()
            sync_directory(failed)
            return True
        except FileNotFoundError:
            return True
        except (OSError, ValueError):
            log.exception("Dropbox could not clear a recovered file from Failed")
            return False

    def dispose(self, root: Path, key: str, tracked: dict[str, Any], item: dict[str, Any]) -> bool:
        """Move a settled input aside before verifying it; never discard changed bytes."""
        path = root / key
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                return True
            signature = (info.st_mtime_ns, info.st_size)
            if list(signature) != tracked["signature"]:
                return True
            failed = root / "Failed"
            if failed.is_symlink():
                raise ValueError("Dropbox/Failed must not be a symlink")
            failed.mkdir(exist_ok=True)
            target = failed / path.name
            kept = target.with_name(target.name + KIND_SUFFIX)
            reason = target.with_name(reason_name(target.name))
            if any(os.path.lexists(path) for path in (target, kept, reason)):
                folder = failed / uuid.uuid4().hex[:8]
                folder.mkdir()
                target = folder / path.name
                kept = target.with_name(target.name + KIND_SUFFIX)
            reason = target.with_name(reason_name(target.name))
            message = (
                item.get("error")
                or "Processing was interrupted. Move this file back to Dropbox to retry."
            )
            create_exclusive_bytes(reason, (message + "\n").encode(), mode=0o600)
            sidecar = path.with_name(path.name + KIND_SUFFIX)
            owned = tracked.get("sidecar") not in (None, [0, -1]) and (
                list(self.sidecar_signature(sidecar)) == tracked["sidecar"]
            )
            os.rename(path, target)
            if owned:
                os.rename(sidecar, kept)
            if item["state"] not in COMPLETED:
                item["failed_path"] = str(target.relative_to(root))
            sync_directory(path.parent)
            sync_directory(target.parent)
            moved = target.lstat()
            if (moved.st_mtime_ns, moved.st_size) != signature:
                reason.write_text(
                    "The file changed during import. Move it back to Dropbox to retry.\n"
                )
                return True
            if item["state"] in COMPLETED:
                data = self.read_stable(target, signature)
                if data is None or hashlib.sha256(data).hexdigest() != tracked["id"]:
                    reason.write_text(
                        "The file changed during import. Move it back to Dropbox to retry.\n"
                    )
                    return True
                target.unlink()
                reason.unlink()
                if owned:
                    kept.unlink()
                if target.parent != failed:
                    target.parent.rmdir()
                sync_directory(failed)
            return True
        except FileNotFoundError:
            return not path.exists()
        except (OSError, ValueError):
            log.exception("Dropbox could not move processed file")
            return False

    async def scan(self) -> None:
        root = self.context.store.notes_root / self.context.config().dropbox_folder
        if not root.exists():
            self.seen.clear()
            return
        if root.is_symlink() or any(p.is_symlink() for p in root.parents):
            raise ValueError("Dropbox must not be a symlink")
        async with self.context.lock:
            records = self.records()
            before = copy.deepcopy(records)
            for item in records["items"].values():
                if (
                    item.get("failed_path")
                    and item["state"] in COMPLETED
                    and await asyncio.to_thread(self.clear_failed, root, item)
                ):
                    item.pop("failed_path")
                    history = records["history"].get(item["id"])
                    if history:
                        history.pop("failed_path", None)
            for key, tracked in list(records["paths"].items()):
                item = records["items"].get(tracked["id"]) or records["history"].get(tracked["id"])
                if (
                    item
                    and item["state"] in COMPLETED | {"failed", "unsupported"}
                    and await asyncio.to_thread(self.dispose, root, key, tracked, item)
                ):
                    del records["paths"][key]
            if records != before:
                self.save(records)
        present = set()
        count = 0
        records = self.records()
        for path, markers in self.candidates(root):
            name = path.name
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
            observed = self.observe(info, path)
            if key in records["paths"] and records["observations"].get(key) == list(observed):
                continue
            old = self.seen.get(key)
            if old is None or old[0] != observed:
                self.seen[key] = (observed, time.monotonic())
                log.info("Dropbox park file: waiting for stable bytes")
                continue
            if time.monotonic() - old[1] < 30:
                continue
            async with self.context.lock:
                records = self.records()
                error = ""
                if info.st_size > MAX_UPLOAD:
                    error = "The file exceeds the 200 MB intake limit."
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
                                    "The file instructions could not be understood. "
                                    "Check them and try again."
                                )
                        if (
                            marker is None
                            and self.handler(data, name) is None
                            and path.suffix.lower() in ARTIFACT_SUFFIXES
                        ):
                            marker = {"kind": "artifact"}
                        submitted = await asyncio.to_thread(self.submit, name, data, marker)
                        records = self.records()
                        if submitted["id"] in records["items"]:
                            records["items"][submitted["id"]]["original_path"] = key
                        records["paths"][key] = {
                            "id": submitted["id"],
                            "signature": list(signature),
                            "sidecar": list(observed[2:]),
                        }
                    except ValueError as exc:
                        error = str(exc)
                    except OSError as exc:
                        error = f"File could not be read: {exc.strerror or 'I/O error'}"
                if error:
                    identity = hashlib.sha256(f"{key}:{signature}".encode()).hexdigest()
                    records["items"][identity] = {
                        "id": identity,
                        "filename": name,
                        "original_path": key,
                        "state": "failed",
                        "parts": [],
                        "error": error,
                    }
                    records["paths"][key] = {
                        "id": identity,
                        "signature": list(signature),
                        "sidecar": list(observed[2:]),
                    }
                    log.info("Dropbox fail file: intake rejected")
                records["observations"][key] = list(observed)
                self.save(records)
        async with self.context.lock:
            self.seen = {key: value for key, value in self.seen.items() if key in present}
            records = self.records()
            observations = {k: v for k, v in records["observations"].items() if k in present}
            if observations != records["observations"]:
                records["observations"] = observations
                self.save(records)
