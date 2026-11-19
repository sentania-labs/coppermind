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


async def cut(source: Path, target: Path, start: float, length: float) -> None:
    # Decode at the boundary so the timestamps and cuts do not depend on codec keyframes.
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
        str(target),
    )
