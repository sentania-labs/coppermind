"""Probe and cut recordings before sending them to a bounded transcription job."""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

from coppermind_store.model_provider import ProviderError


async def _run(*args: str) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, _ = await process.communicate()
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
    except OSError as exc:
        raise ProviderError("Audio processing is unavailable.", "recording-unreadable") from exc
    if process.returncode:
        raise ProviderError(
            "The recording could not be read for splitting.", "recording-unreadable"
        )
    return out


async def duration(path: Path) -> float:
    out = await _run(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    )
    try:
        value = float(out.strip())
        if not math.isfinite(value) or value <= 0:
            raise ValueError
        return value
    except ValueError as exc:
        raise ProviderError(
            "The recording's duration could not be determined.", "recording-unreadable"
        ) from exc


# Containers accepted by the transcription API and supported by ffmpeg stream copy.
_COPY_FORMATS = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
    ".aac": "audio/aac",
}


async def cut(
    source: Path, target: Path, start: float, length: float, filename: str
) -> tuple[Path, str]:
    """Copy compressed audio where its container permits it, otherwise encode FLAC."""
    suffix = Path(filename).suffix.lower()
    if suffix in _COPY_FORMATS:
        part = target.with_suffix(suffix)
        try:
            await _run(
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-ss",
                str(start),
                "-i",
                str(source),
                "-t",
                str(length),
                "-map",
                "0:a:0",
                "-vn",
                "-c:a",
                "copy",
                "-y",
                str(part),
            )
            # Packet boundaries can add duration. Leave a margin under the job limit.
            measured = await duration(part)
            if measured <= length + 0.9:
                return part, _COPY_FORMATS[suffix]
        except ProviderError:
            pass
        part.unlink(missing_ok=True)
    part = target.with_suffix(".flac")
    await _run(
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(source),
        "-ss",
        str(start),
        "-t",
        str(length),
        "-vn",
        "-c:a",
        "flac",
        "-y",
        str(part),
    )
    return part, "audio/flac"
