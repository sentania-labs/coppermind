"""Everything handed to Coppermind, as one list: recordings, calendar files, documents.

The three kinds are stored in three places (transcription intakes, calendar
import receipts and artifacts). Admin shows them as the one thing they are to
the person who handed them in, and every count of them comes from this module.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from coppermind.db.models import Artifact
from coppermind.store_protocol import ArtifactMissing, StoreError
from coppermind_store.admin_features import recording_minutes, took_minutes
from coppermind_store.ai_services import UNREACHABLE

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore

WINDOW = timedelta(days=60)
RECORDING_STATES = {
    "queued": "waiting",
    "running": "working",
    "succeeded": "done",
    "failed": "failed",
}
AUDIO_SUFFIXES = {".m4a", ".mp3", ".wav", ".ogg", ".opus", ".flac", ".aac", ".webm", ".mp4"}
CALENDAR_SUFFIXES = {".ics", ".zip"}


def kind_of(filename: str) -> str:
    """What a file is, from its name: a recording, a calendar file or a document."""
    suffix = Path(filename).suffix.lower()
    if suffix in CALENDAR_SUFFIXES:
        return "calendar"
    if suffix in AUDIO_SUFFIXES - {".mp4", ".webm"}:
        return "recording"
    return "document"


def _recent(stamp: str | datetime) -> bool:
    try:
        moment = datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return datetime.now(UTC) - moment <= WINDOW


class AdminIncoming:
    def __init__(self, store: LocalStore) -> None:
        self.store = store

    @property
    def context(self) -> Any:
        return self.store.context

    # -- recordings ------------------------------------------------------------------------

    def recording(self, job: Any) -> dict[str, Any]:
        speakers = 0
        with contextlib.suppress(Exception):
            saved = self.context.recordings.saved_transcript(job) if job.transcribed else {}
            speakers = len(
                {s.get("speaker") for s in saved.get("segments", []) if s.get("speaker")}
            )
        held = job.state == "failed" and job.error_rule in UNREACHABLE and not job.dismissed
        return {
            "id": "r:" + job.id,
            "artifact_id": job.artifact_id,
            "kind": "recording",
            "title": job.title,
            "filename": job.filename,
            "minutes": recording_minutes(job),
            "when": job.created_at,
            # A recording that stopped only because its service did not answer is waiting,
            # not failed: it is sent again by itself once the service answers.
            "state": "waiting" if held else RECORDING_STATES[job.state],
            "held": held,
            "error": job.error,
            "rule": job.error_rule,
            "retry_after": job.retry_after if job.attempts < job.attempt_limit else "",
            "attempts": job.attempts,
            "attempt_limit": job.attempt_limit,
            "started_at": job.started_at,
            "done_at": job.succeeded_at,
            "took_minutes": took_minutes(job) if job.state == "succeeded" else None,
            "speakers": speakers,
            "note_id": self.context.recordings.parent_note_id(job),
            "audio_removed": job.audio_removed,
            "dismissed": job.dismissed,
            "size": job.size_bytes,
        }

    def recordings(self) -> list[dict[str, Any]]:
        return [
            self.recording(job)
            for job in self.context.recordings.jobs()
            if _recent(job.created_at) and not job.dismissed
        ]

    # -- the one list ----------------------------------------------------------------------

    async def documents(self) -> list[dict[str, Any]]:
        cutoff = datetime.now(UTC) - WINDOW
        async with self.store.session_factory() as session:
            rows = (
                await session.scalars(
                    sa.select(Artifact)
                    # A file written from a recording (its transcript) was not handed in.
                    .where(
                        Artifact.received_at >= cutoff,
                        Artifact.url.is_(None),
                        Artifact.source_id.is_(None),
                    )
                    .order_by(Artifact.received_at.desc())
                    .limit(500)
                )
            ).all()
        return [
            {
                "id": "a:" + row.id,
                "kind": "document",
                "title": row.filename or row.title,
                "when": row.received_at.isoformat(),
                "state": "done",
                "searchable": row.text_status == "extracted",
                "size": row.size_bytes,
            }
            for row in rows
            if not (row.mime_type or "").startswith("audio/")
        ]

    def calendar_files(self) -> list[dict[str, Any]]:
        from coppermind_store.admin_calendar import file_counts

        counts = file_counts(self.context.archive)
        out = []
        for receipt in self.context.archive.receipts():
            if receipt.state == "failed" and receipt.dismissed:
                continue
            if not _recent(receipt.received_at):
                continue
            out.append(
                {
                    "id": "c:" + receipt.id,
                    "kind": "calendar",
                    "title": receipt.export_filename or receipt.filename,
                    "when": receipt.received_at,
                    "state": {"succeeded": "done", "failed": "failed", "pending": "working"}[
                        receipt.state
                    ],
                    "error": receipt.error,
                    "new": counts.get(receipt.id, {}).get("adds", 0),
                }
            )
        return out

    def refused_drops(self) -> list[dict[str, Any]]:
        """Files in the drop folder that could not be taken in at all."""
        out = []
        for item in self.context.dropbox.snapshot()["items"]:
            if item["state"] not in {"failed", "unsupported"}:
                continue
            if any(part.get("import_id") for part in item.get("parts", [])):
                # A calendar file that failed is listed from its import receipt.
                continue
            out.append(
                {
                    "id": "d:" + item["id"],
                    "kind": kind_of(item["filename"]),
                    "title": item["filename"],
                    "when": item.get("received_at", ""),
                    "state": "failed",
                    "error": item.get("error", ""),
                    "unsupported": item["state"] == "unsupported",
                }
            )
        return out

    async def listing(self) -> list[dict[str, Any]]:
        items = [
            *self.recordings(),
            *await asyncio.to_thread(self.calendar_files),
            *await self.documents(),
            *self.refused_drops(),
        ]
        order = {"failed": 0, "waiting": 1, "working": 2, "done": 3}
        items.sort(key=lambda item: item["when"], reverse=True)
        items.sort(key=lambda item: order[item["state"]])
        return items

    def failed(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [item for item in items if item["state"] == "failed"]

    # -- one item --------------------------------------------------------------------------

    async def detail(self, payload: dict[str, Any]) -> dict[str, Any]:
        identity = str(payload["id"])
        prefix, _, key = identity.partition(":")
        if prefix == "r":
            job = self.context.recordings.load(key)
            result = self.recording(job)
            result["has_audio"] = not job.audio_removed and job.state != "succeeded"
            return result
        items = await self.listing()
        found = next((item for item in items if item["id"] == identity), None)
        if found is None:
            raise ValueError("That item is no longer listed.")
        return found

    async def retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = str(payload["id"]).removeprefix("r:")
        await self.context.recordings.retry({"id": key})
        return {"title": self.context.recordings.load(key).title}

    async def delete_recording(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Delete a recording's audio for good. A note already written from it stays."""
        key = str(payload["id"]).removeprefix("r:")
        async with self.context.lock:
            job = self.context.recordings.load(key)
        from coppermind_store import artifacts

        if not job.audio_removed:
            with contextlib.suppress(ArtifactMissing):
                await artifacts.remove_artifact(self.store, job.artifact_id)
        async with self.context.lock:
            job = self.context.recordings.load(key)
            job.audio_removed = True
            job.dismissed = True
            if job.state in {"queued", "running"}:
                job.state = "failed"
                job.set_error("You deleted this recording.", "recording-deleted")
            self.context.recordings.save(job)
        return {"title": job.title}

    async def remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Remove a document, or clear away a file that was never taken in."""
        identity = str(payload["id"])
        prefix, _, key = identity.partition(":")
        if prefix == "a":
            record = await self.store.get_artifact(key)
            await self.store.remove_artifact(key)
            return {"title": record.filename or record.title, "kind": "document"}
        if prefix == "d":
            item = self.context.dropbox.records()["items"].get(key)
            if item is None:
                raise ValueError("That file is no longer listed.")
            await self._discard_drop(item)
            await self.context.dropbox.dismiss({"id": key})
            return {"title": item["filename"], "kind": "refused"}
        if prefix == "c":
            receipt = next((r for r in self.context.archive.receipts() if r.id == key), None)
            if receipt is None:
                raise ValueError("That file is no longer listed.")
            if receipt.state != "failed":
                raise ValueError("A calendar file that was read stays in past imports.")
            await self.context.dismiss_import({"id": key})
            for item in self.context.dropbox.records()["history"].values():
                if any(part.get("import_id") == key for part in item.get("parts", [])):
                    await self._discard_drop(item)
            return {"title": receipt.export_filename or receipt.filename, "kind": "refused"}
        raise ValueError("That item cannot be removed here.")

    async def _discard_drop(self, item: dict[str, Any]) -> None:
        """Take a refused file out of the drop folder, where it was moved to Failed."""
        root = self.store.notes_root / self.context.config().dropbox_folder
        for name in (item.get("failed_path"), item.get("original_path")):
            if not name:
                continue
            target = (root / name).resolve()
            if root.resolve() not in target.parents:
                continue
            with contextlib.suppress(OSError):
                await asyncio.to_thread(target.unlink)
                reason = target.with_name(target.name + ".reason.txt")
                await asyncio.to_thread(reason.unlink, True)

    # -- handing a file in from Admin ------------------------------------------------------

    async def accept(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Take in a file Admin has already stored as an artifact, by what it is."""
        artifact_id = str(payload["artifact_id"])
        record = await self.store.get_artifact(artifact_id)
        filename = record.filename or record.title
        if (record.mime_type or "").startswith("audio/"):
            status = await self.context.recordings.accept(
                {
                    "artifact_id": artifact_id,
                    "provider": "admin",
                    "external_source_id": record.sha256 or artifact_id,
                    "title": Path(filename).stem,
                    "origin": "admin",
                }
            )
            return {
                "kind": "recording",
                "title": Path(filename).stem,
                "id": "r:" + status["id"],
                "transcribing": self.context.recordings.enabled()
                and not (self.context.recordings.assignment() or _Off()).paused,
            }
        return {"kind": "document", "title": filename, "id": "a:" + artifact_id}

    async def replace_failed(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A fresh file was added in place of one that could not be read: clear the old one."""
        try:
            return await self.remove({"id": str(payload["id"])})
        except (ValueError, StoreError):
            return {"title": "", "kind": "refused"}


class _Off:
    paused = False
