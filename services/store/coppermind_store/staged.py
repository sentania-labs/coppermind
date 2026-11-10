"""Staged uploads: one file larger than the inline limit, streamed to disk.

`POST /v1/artifacts` holds its whole body in memory, so it stops at
`limits.artifact_max_bytes`. A bigger file comes through here instead, in three
steps that each fit in one request:

1. Reserve. The declaration names the filename, exact byte count and lowercase
   sha256 of the file, plus the same fields the inline route accepts (the media
   type among them). The receipt is written durably and its id comes back.
2. Stream. The body arrives as an async byte iterator and goes to a temporary
   file in the staging directory, hashed and counted in 1 MiB steps with the file
   work off the event loop. Only a stream whose count and digest both match the
   declaration replaces the staged file, so a bad upload never destroys a
   verified one, and no step holds more than a chunk or two of the file.
3. Finalize. The staged file is verified again and copied, in chunks, into the
   exact same write path an inline upload takes (`artifacts._store_revision`), so
   the archive, the browsable copy, the record, the citation and replay behaviour
   are identical.

The receipt is the only durable record of a reservation, under
`<data>/state/staged/<id>.json`, beside the other state files. Every reservation
is swept once `retention.days` has passed since it was reserved (an upload that
never completed) or last moved (a `stored` or `failed` receipt), together with
its staged copy and any temporary file a crash left behind. The sweep runs on the
next reservation.

Identity works the way the inline contract does. The same declaration returns the
same receipt and the same stored artifact; identical bytes replay, and different
bytes under one receipt identifier are a conflict rather than a second revision.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coppermind.atomicio import atomic_write_bytes, sync_directory
from coppermind.ids import new_id
from coppermind.logging import get_logger
from coppermind.settings import ProductSettings
from coppermind.store_protocol import (
    ReceiptMissing,
    StagedFileUnavailable,
    StagedLimits,
    StagedReceipt,
    StagedReservation,
    ValidationFailed,
)

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore

log = get_logger("coppermind-store")

RECEIPT_SCHEMA = 1
# How much of a stream is hashed and written at a time. One number keeps the
# memory ceiling of a staged upload independent of the file's size.
CHUNK_BYTES = 1 << 20

# The declared artifact fields, in the order the declaration lists them.
_FIELD_ORDER = ("title", "date", "tags", "note_id", "origin", "artifact_id", "url", "mime_type")


def _json_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _staged_dir(store: LocalStore) -> Path:
    return store.control.store.state_dir / "staged"


def _receipt_path(store: LocalStore, receipt_id: str) -> Path:
    return _staged_dir(store) / f"{receipt_id}.json"


def _bytes_path(store: LocalStore, receipt_id: str) -> Path:
    return _staged_dir(store) / f"{receipt_id}.bin"


def _write_receipt(store: LocalStore, document: dict[str, Any]) -> None:
    try:
        atomic_write_bytes(_receipt_path(store, str(document["id"])), _json_bytes(document))
    except OSError as exc:
        raise StagedFileUnavailable(str(exc)) from exc


def _read_receipt(store: LocalStore, receipt_id: str) -> dict[str, Any]:
    if not receipt_id or "\0" in receipt_id:
        raise ReceiptMissing(receipt_id)
    path = _receipt_path(store, receipt_id)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReceiptMissing(receipt_id) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StagedFileUnavailable(f"the staged receipt {receipt_id} is unreadable") from exc
    if not isinstance(document, dict) or document.get("id") != receipt_id:
        raise StagedFileUnavailable(f"the staged receipt {receipt_id} is invalid")
    return document


def _limits(settings: ProductSettings) -> StagedLimits:
    return StagedLimits(
        inline_max_bytes=settings.limits.artifact_max_bytes,
        staged_max_bytes=settings.limits.staged_max_bytes,
    )


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _receipt(document: dict[str, Any]) -> StagedReceipt:
    return StagedReceipt.model_validate(document)


def _fields_document(request: StagedReservation) -> dict[str, Any]:
    """The declared artifact fields, with an unset optional left out rather than null."""
    declared = request.fields.model_dump(mode="json")
    payload: dict[str, Any] = {}
    for key in _FIELD_ORDER:
        value = declared.get(key)
        if value is None or value == []:
            continue
        payload[key] = value
    return payload


def _document_for(
    receipt_id: str,
    request: StagedReservation,
    limits: StagedLimits,
    *,
    now: datetime,
) -> dict[str, Any]:
    """The receipt body: the declaration whole, plus what has happened to it."""
    return {
        "schema_version": RECEIPT_SCHEMA,
        "id": receipt_id,
        "state": "uploading",
        "filename": request.filename,
        "size_bytes": request.size_bytes,
        "sha256": request.sha256,
        "upload_bytes": 0,
        "limits": limits.model_dump(mode="json"),
        "fields": _fields_document(request),
        "failure": None,
        "artifact": None,
        "reserved_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }


def _same_declaration(document: dict[str, Any], declared: dict[str, Any]) -> bool:
    """Whether a stored receipt answers exactly this declaration.

    A completed receipt carries what happened to it: how many bytes landed, the
    artifact it became, when it moved. Only what was declared decides a replay,
    so an identical retry finds the receipt even after finalize.
    """
    declared_keys = (
        "filename",
        "size_bytes",
        "sha256",
        "limits",
        "fields",
    )
    return all(document.get(key) == declared.get(key) for key in declared_keys)


def _load(path: Path) -> dict[str, Any] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _expired(document: dict[str, Any], after: int, now: datetime) -> bool:
    key = "reserved_at" if document.get("state") == "uploading" else "updated_at"
    moved = _time(document.get(key))
    return moved is not None and (now - moved).total_seconds() > after


def _survey(store: LocalStore, now: datetime) -> list[dict[str, Any]]:
    """Every receipt that survives the retention window, after dropping those past it.

    A receipt past the window goes with its staged copy, whatever its state, and
    so does any temporary file a crashed upload left behind. With retention off
    nothing is dropped.
    """
    window = store.control.settings().retention.days
    after = window * 24 * 3600 if window > 0 else None
    kept: list[dict[str, Any]] = []
    for path in _receipts(store):
        document = _load(path)
        if document is None:
            continue
        if after is not None and _expired(document, after, now):
            receipt_id = str(document.get("id", path.stem))
            try:
                _bytes_path(store, receipt_id).unlink(missing_ok=True)
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise StagedFileUnavailable(str(exc)) from exc
            log.info("staged upload receipt expired", receipt_id=receipt_id)
            continue
        kept.append(document)
    if after is not None:
        _drop_orphans(store, after, now)
    return kept


def _drop_orphans(store: LocalStore, after: int, now: datetime) -> None:
    try:
        for path in _staged_dir(store).glob(".*.tmp"):
            try:
                if now.timestamp() - path.stat().st_mtime > after:
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                continue
    except OSError as exc:
        raise StagedFileUnavailable(str(exc)) from exc


def _receipts(store: LocalStore) -> list[Path]:
    try:
        return sorted(_staged_dir(store).glob("*.json"))
    except OSError as exc:
        raise StagedFileUnavailable(str(exc)) from exc


async def reserve(store: LocalStore, request: StagedReservation) -> StagedReceipt:
    """Record what a file will be, and return the receipt its bytes stream into."""
    settings = store.control.settings()
    limits = _limits(settings)
    if request.size_bytes > limits.staged_max_bytes:
        raise ValidationFailed(
            [
                "size_bytes: above the staged maximum of "
                f"{limits.staged_max_bytes} bytes; split or compress the file"
            ]
        )
    now = _now()
    declared = _document_for("x", request, limits, now=now)
    wanted = {key: value for key, value in declared.items() if key != "schema_version"}
    async with store._artifact_lock:
        for document in _survey(store, now):
            if _same_declaration(document, wanted):
                return _receipt(document)
        document = dict(wanted, id=new_id())
        _write_receipt(store, document)
    return _receipt(document)


async def put_bytes(
    store: LocalStore,
    receipt_id: str,
    data: AsyncIterator[bytes],
    *,
    declared_bytes: int | None = None,
) -> StagedReceipt:
    """Stream one body into the staging area, then verify its count and digest.

    The receipt lock is held only to read the declaration and again to record the
    outcome, never while the body is arriving, so a slow client stalls nothing.
    """
    async with store._artifact_lock:
        document = _read_receipt(store, receipt_id)
    declared = int(document["size_bytes"])
    expected = str(document["sha256"])
    problem: str | None = None
    temp: Path | None = None
    if declared_bytes is not None and declared_bytes != declared:
        problem = (
            f"Content-Length announced {declared_bytes} bytes; the reservation declared {declared}"
        )
    else:
        temp, written, digest = await _stream_to_disk(store, receipt_id, data, declared)
        if written != declared:
            problem = f"the stream carried {written} bytes; the reservation declared {declared}"
        elif digest != expected:
            problem = f"the bytes hashed to {digest}; the reservation declared {expected}"
    try:
        async with store._artifact_lock:
            document = _read_receipt(store, receipt_id)
            stored = document.get("state") == "stored"
            if problem is not None:
                if stored:
                    raise ValidationFailed([problem])
                return _fail(store, document, problem)
            if stored:
                return _receipt(document)
            assert temp is not None
            os.replace(temp, _bytes_path(store, receipt_id))
            temp = None
            sync_directory(_staged_dir(store))
            document["state"] = "uploading"
            document["upload_bytes"] = declared
            document["failure"] = None
            document["updated_at"] = _now().isoformat()
            _write_receipt(store, document)
            return _receipt(document)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _absorb(stream: Any, hasher: Any, chunk: bytes | bytearray) -> None:
    hasher.update(chunk)
    stream.write(chunk)


def _settle(stream: Any) -> None:
    stream.flush()
    os.fsync(stream.fileno())


async def _stream_to_disk(
    store: LocalStore, receipt_id: str, data: AsyncIterator[bytes], declared: int
) -> tuple[Path, int, str]:
    """Write the stream to a temporary file in chunks, counting and hashing as it goes.

    Returns the temporary file, the count of every byte the stream carried and
    the digest of what was written. Once the count passes `declared` nothing more
    is written or hashed, but the stream is still read to its end so the sender
    gets an answer rather than a broken connection.
    """
    folder = _staged_dir(store)
    hasher = hashlib.sha256()
    written = 0
    pending = bytearray()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(dir=folder, prefix=f".{receipt_id}.", suffix=".tmp")
    except OSError as exc:
        raise StagedFileUnavailable(str(exc)) from exc
    temp = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            async for chunk in data:
                written += len(chunk)
                if written > declared:
                    pending.clear()
                    continue
                pending += chunk
                if len(pending) >= CHUNK_BYTES:
                    await asyncio.to_thread(_absorb, stream, hasher, pending)
                    pending.clear()
            if pending:
                await asyncio.to_thread(_absorb, stream, hasher, pending)
            await asyncio.to_thread(_settle, stream)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise StagedFileUnavailable(str(exc)) from exc
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return temp, written, hasher.hexdigest()


def _discard_staged(store: LocalStore, receipt_id: str) -> None:
    try:
        _bytes_path(store, receipt_id).unlink(missing_ok=True)
    except OSError as exc:
        raise StagedFileUnavailable(str(exc)) from exc


def _fail(store: LocalStore, document: dict[str, Any], reason: str) -> StagedReceipt:
    document["state"] = "failed"
    document["upload_bytes"] = 0
    document["failure"] = reason
    document["updated_at"] = _now().isoformat()
    _write_receipt(store, document)
    return _receipt(document)


async def finalize(store: LocalStore, receipt_id: str) -> StagedReceipt:
    """Verify the staged bytes, then store the artifact through the inline code path.

    The receipt is read and verified under `store._artifact_lock`, and written
    again under it once the artifact exists. The artifact write itself is made by
    `create_staged_artifact`, which takes that lock itself, so it runs between the
    two: an `asyncio.Lock` is not reentrant. The file is verified and copied in
    chunks, so finalizing never holds more of it than a chunk.
    """
    from coppermind_store.artifacts import create_staged_artifact

    async with store._artifact_lock:
        document = _read_receipt(store, receipt_id)
        if document.get("state") == "stored" and document.get("artifact"):
            return _receipt(document)
        if document.get("state") == "failed":
            raise ValidationFailed([str(document.get("failure") or "the upload failed")])
        declared = int(document["size_bytes"])
        path = _bytes_path(store, receipt_id)
        if not path.is_file():
            raise ValidationFailed(
                ["no bytes are staged for this receipt; send the PUT, then finalize"]
            )
        size, digest = await asyncio.to_thread(_read_staged, path, declared)
        reason: str | None = None
        if size != declared:
            reason = _mismatch("the staged file", size, declared)
        elif digest != str(document["sha256"]):
            reason = (
                f"the staged file hashes to {digest}; the reservation declared {document['sha256']}"
            )
        if reason is not None:
            # The receipt keeps the reason, and the same answer goes out as the
            # validation error the route reports: a failed upload is not a 200.
            _fail(store, document, reason)
            _discard_staged(store, receipt_id)
            raise ValidationFailed([reason])
        declaration = StagedReservation.model_validate(
            {
                "filename": document["filename"],
                "size_bytes": declared,
                "sha256": digest,
                "fields": document.get("fields") or {},
            }
        )
    try:
        created = await create_staged_artifact(store, path, declaration, sha256=digest)
    except FileNotFoundError as exc:
        async with store._artifact_lock:
            current = _read_receipt(store, receipt_id)
        if current.get("state") == "stored" and current.get("artifact"):
            return _receipt(current)
        raise StagedFileUnavailable(str(exc)) from exc
    async with store._artifact_lock:
        document = _read_receipt(store, receipt_id)
        if document.get("state") == "stored" and document.get("artifact"):
            return _receipt(document)
        document["state"] = "stored"
        document["upload_bytes"] = size
        document["failure"] = None
        document["artifact"] = created.model_dump(mode="json")
        document["updated_at"] = _now().isoformat()
        _write_receipt(store, document)
        _discard_staged(store, receipt_id)
    return _receipt(document)


def _mismatch(name: str, got: int, wanted: int) -> str:
    return f"{name} holds {got} bytes; the reservation declared {wanted}"


def _read_staged(path: Path, declared: int) -> tuple[int, str]:
    """The staged file's byte count and digest, reading no more than the declaration."""
    hasher = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(CHUNK_BYTES):
                size += len(chunk)
                if size > declared:
                    return size, hasher.hexdigest()
                hasher.update(chunk)
    except OSError as exc:
        raise StagedFileUnavailable(str(exc)) from exc
    return size, hasher.hexdigest()


async def get_receipt(store: LocalStore, receipt_id: str) -> StagedReceipt:
    """What one reservation looks like now, for a caller deciding whether to retry."""
    async with store._artifact_lock:
        document = _read_receipt(store, receipt_id)
    return _receipt(document)
