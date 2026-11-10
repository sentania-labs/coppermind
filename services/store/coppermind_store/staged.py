"""Staged uploads: one file larger than the inline limit, streamed to disk.

`POST /v1/artifacts` holds its whole body in memory, so it stops at
`limits.artifact_max_bytes`. A bigger file comes through here instead, in three
steps that each fit in one request:

1. Reserve. The declaration names the filename, media type, exact byte count and
   lowercase sha256 of the file, plus the same fields the inline route accepts.
   The receipt is written durably and its id comes back.
2. Stream. The bytes go into the staging directory in fixed-size chunks, hashed
   and counted as they land. The count and digest are compared against the
   declaration, and the first mismatch refuses the upload.
3. Finalize. The staged file is handed to the exact same code path an inline
   upload takes (`artifacts.create_artifact`), so the archive, the browsable
   copy, the record, the citation and replay behaviour are identical.

The receipt is the only durable record of a reservation, under
`<data>/state/staged/<id>.json`, beside the other state files. A reservation whose
upload never completed is dropped once it is older than the retention window, and
the next write performs that sweep; a `stored` or `failed` receipt stays for the
numbered retention pass, the way Dropbox receipts do.

Identity works the way the inline contract does. The same declaration returns the
same receipt and the same stored artifact; identical bytes replay, and different
bytes under one receipt identifier are a conflict rather than a second revision.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coppermind.atomicio import atomic_write_bytes, sync_directory
from coppermind.ids import new_id
from coppermind.logging import get_logger
from coppermind.settings import ProductSettings
from coppermind.store_protocol import (
    CreateArtifact,
    CreatedArtifact,
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

# The declared artifact fields, in the order `CreateArtifact` reads them. `filename`
# and `mime_type` come from the receipt's own fields, `content_base64` from the
# staged file.
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
    if not receipt_id:
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
        "mime_type": request.mime_type,
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
        "mime_type",
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


def _sweep(store: LocalStore, now: datetime) -> None:
    """Drop reservations whose upload never completed, once the retention window passes."""
    window = store.control.settings().retention.days
    if window <= 0:
        return
    after = window * 24 * 3600
    for path in _receipts(store):
        document = _load(path)
        if document is None or document.get("state") != "uploading":
            continue
        reserved_at = _time(document.get("reserved_at"))
        if reserved_at is None or (now - reserved_at).total_seconds() <= after:
            continue
        receipt_id = str(document.get("id", path.stem))
        try:
            _bytes_path(store, receipt_id).unlink(missing_ok=True)
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise StagedFileUnavailable(str(exc)) from exc
        log.info("staged upload reservation expired", receipt_id=receipt_id)


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
        _sweep(store, now)
        for path in _receipts(store):
            document = _load(path)
            if document is not None and _same_declaration(document, wanted):
                return _receipt(document)
        document = dict(wanted, id=new_id())
        _write_receipt(store, document)
    return _receipt(document)


async def put_bytes(
    store: LocalStore, receipt_id: str, data: bytes, *, declared_bytes: int | None = None
) -> StagedReceipt:
    """Stream one body into the staging area, then verify its count and digest."""
    async with store._artifact_lock:
        document = _read_receipt(store, receipt_id)
        declared = int(document["size_bytes"])
        if declared_bytes is not None and declared_bytes != declared:
            return _fail(
                store,
                document,
                f"Content-Length announced {declared_bytes} bytes; the reservation declared "
                f"{declared}",
            )
        written, digest = await _stream_to_disk(store, receipt_id, data, declared)
        if written != declared:
            _discard_staged(store, receipt_id)
            return _fail(
                store,
                document,
                f"the stream carried {written} bytes; the reservation declared {declared}",
            )
        if digest != str(document["sha256"]):
            _discard_staged(store, receipt_id)
            return _fail(
                store,
                document,
                f"the bytes hashed to {digest}; the reservation declared {document['sha256']}",
            )
        document["state"] = "uploading"
        document["upload_bytes"] = written
        document["failure"] = None
        document["updated_at"] = _now().isoformat()
        _write_receipt(store, document)
        return _receipt(document)


async def _stream_to_disk(
    store: LocalStore, receipt_id: str, data: bytes, declared: int
) -> tuple[int, str]:
    """Write `data` to the staged file in chunks, counting and hashing as it goes."""
    folder = _staged_dir(store)
    folder.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    written = 0
    handle, name = tempfile.mkstemp(dir=folder, prefix=f".{receipt_id}.", suffix=".tmp")
    temp = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            for start in range(0, len(data), CHUNK_BYTES):
                chunk = data[start : start + CHUNK_BYTES]
                stream.write(chunk)
                hasher.update(chunk)
                written += len(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if written > declared:
            temp.unlink(missing_ok=True)
            return written, hasher.hexdigest()
        os.replace(temp, _bytes_path(store, receipt_id))
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    sync_directory(folder)
    return written, hasher.hexdigest()


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

    The receipt is read, verified and written outside `store._artifact_lock`, and
    the artifact write itself is made by `create_artifact`, which takes that lock
    itself. Holding it here as well would stop the very call this step depends
    on, since an `asyncio.Lock` is not reentrant. The order is still the one the
    write protocol asks for: verify the bytes, then let the one artifact write
    path do its own locked work.
    """
    from coppermind_store.artifacts import create_artifact

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
    size, digest = await _read_staged(path, declared)
    if size != declared:
        _discard_staged(store, receipt_id)
        return _fail(store, document, _mismatch("the staged file", size, declared))
    if digest != str(document["sha256"]):
        _discard_staged(store, receipt_id)
        return _fail(
            store,
            document,
            f"the staged file hashes to {digest}; the reservation declared {document['sha256']}",
        )
    created = await create_artifact(
        store, _create_request(store, document), payload_size_bytes=size
    )
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


def _create_request(store: LocalStore, document: dict[str, Any]) -> CreateArtifact:
    """The inline request these bytes become: the declaration with the bytes attached."""
    fields = document.get("fields") or {}
    payload: dict[str, Any] = {"kind": "artifact", "filename": document["filename"]}
    for key in _FIELD_ORDER:
        if key in fields:
            payload[key] = fields[key]
    if document.get("mime_type"):
        payload["mime_type"] = document["mime_type"]
    payload["content_base64"] = _base64_of(_bytes_path(store, str(document["id"])))
    return CreateArtifact.model_validate(payload)


def _base64_of(path: Path) -> str:
    with path.open("rb") as stream:
        return base64.b64encode(stream.read()).decode()


async def _read_staged(path: Path, declared: int) -> tuple[int, str]:
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


def _artifact_of(document: dict[str, Any]) -> CreatedArtifact | None:
    """The stored artifact a completed receipt names, or None when it has none."""
    if document.get("state") != "stored" or not document.get("artifact"):
        return None
    return CreatedArtifact.model_validate(document["artifact"])
