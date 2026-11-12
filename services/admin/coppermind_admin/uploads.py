"""Take one file from this computer without ever holding it whole in memory.

A file handed in from Admin can be a long recording, so it is read as a stream:
the small fields that come first are checked (the form token among them) and the
file itself goes straight to a temporary file while its size and hash are
counted. Nothing of the file is kept unless the token was right.
"""

from __future__ import annotations

import hashlib
import secrets
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, cast

from fastapi import Request

FIELD_LIMIT = 16384


class UploadRefused(Exception):
    """The upload was not taken. The message is for the person who sent it."""

    limit: int = 0


@dataclass
class Upload:
    fields: dict[str, str] = field(default_factory=dict)
    filename: str = ""
    path: Path | None = None
    size: int = 0
    sha256: str = ""

    def discard(self) -> None:
        if self.path is not None:
            self.path.unlink(missing_ok=True)


class _Reader:
    def __init__(self, stream: AsyncIterator[bytes]) -> None:
        self.stream = stream
        self.buffer = bytearray()
        self.done = False

    async def fill(self) -> bool:
        if self.done:
            return False
        try:
            self.buffer.extend(await anext(self.stream))
            return True
        except StopAsyncIteration:
            self.done = True
            return False

    async def until(self, marker: bytes, sink: IO[bytes] | None, limit: int) -> bytes:
        """Read up to `marker`. Bytes before it go to `sink`, or come back when there is none."""
        kept = bytearray()
        taken = 0
        while True:
            at = self.buffer.find(marker)
            if at >= 0:
                chunk = bytes(self.buffer[:at])
                del self.buffer[: at + len(marker)]
            else:
                # Keep a tail the marker could straddle, and pass the rest on.
                safe = max(0, len(self.buffer) - len(marker) + 1)
                chunk = bytes(self.buffer[:safe])
                del self.buffer[:safe]
            taken += len(chunk)
            if taken > limit:
                raise UploadRefused("too large")
            if sink is not None:
                sink.write(chunk)
            else:
                kept.extend(chunk)
            if at >= 0:
                return bytes(kept)
            if not await self.fill():
                raise UploadRefused("The upload was cut short. Try again.")


class _Counting:
    """Writes the file through while counting its size and hash."""

    def __init__(self, handle: IO[bytes], upload: Upload) -> None:
        self.handle = handle
        self.upload = upload
        self.digest = hashlib.sha256()

    def write(self, chunk: bytes) -> int:
        self.digest.update(chunk)
        self.upload.size += len(chunk)
        return self.handle.write(chunk)


def _disposition(headers: bytes) -> tuple[str, str | None]:
    name, filename = "", None
    for line in headers.decode("utf-8", errors="replace").split("\r\n"):
        if not line.lower().startswith("content-disposition:"):
            continue
        for part in line.split(";")[1:]:
            key, _, value = part.strip().partition("=")
            value = value.strip().strip('"')
            if key == "name":
                name = value
            elif key == "filename":
                filename = value.replace("\\", "/").rsplit("/", 1)[-1]
    return name, filename


async def receive(request: Request, limit: int | Callable[[str], int]) -> Upload:
    """Read a multipart form whose last part is the file. Raises `UploadRefused`."""
    content_type = request.headers.get("content-type", "")
    if "boundary=" not in content_type:
        raise UploadRefused("Choose a file first.")
    boundary = content_type.split("boundary=", 1)[1].split(";")[0].strip().strip('"').encode()
    expected = getattr(request.state, "csrf_expected", "")
    reader = _Reader(request.stream().__aiter__())
    upload = Upload()
    await reader.until(b"--" + boundary, None, FIELD_LIMIT)
    delimiter = b"\r\n--" + boundary
    try:
        while True:
            while len(reader.buffer) < 2 and await reader.fill():
                pass
            if bytes(reader.buffer[:2]) == b"--" or not reader.buffer:
                break
            headers = await reader.until(b"\r\n\r\n", None, FIELD_LIMIT)
            name, filename = _disposition(headers)
            if filename is None:
                value = await reader.until(delimiter, None, FIELD_LIMIT)
                upload.fields[name] = value.decode("utf-8", errors="replace")
                continue
            supplied = upload.fields.get("csrf", "")
            if not expected or not secrets.compare_digest(supplied.encode(), expected.encode()):
                raise UploadRefused(
                    "This page was open too long, so Admin could not be sure the file came "
                    "from you. Reload the page and add the file again."
                )
            if not filename:
                raise UploadRefused("Choose a file first.")
            if upload.path is not None:
                raise UploadRefused("Add one file at a time. Nothing was added.")
            handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed below, kept on disk
                prefix="coppermind-upload-", delete=False
            )
            upload.path, upload.filename = Path(handle.name), filename
            sink = _Counting(handle, upload)
            cap = limit(filename) if callable(limit) else limit
            try:
                await reader.until(delimiter, cast(IO[bytes], sink), cap)
            except UploadRefused as exc:
                if str(exc) == "too large":
                    exc.limit = cap
                raise
            finally:
                handle.close()
            upload.sha256 = sink.digest.hexdigest()
    except BaseException:
        upload.discard()
        raise
    if upload.path is None or not upload.size:
        upload.discard()
        raise UploadRefused("Choose a file first.")
    return upload
