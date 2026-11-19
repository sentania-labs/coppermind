"""Exercise real audio cuts and the recording worker's stitched transcript."""

import asyncio
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from coppermind_store import artifacts, audio_parts
from coppermind_store.model_provider import ProviderError
from coppermind_store.recordings import Recordings, TranscriptionJob
from coppermind_store.voice_store import VoiceStore

from coppermind.context_config import FeatureAssignment


def make_audio(path, seconds):
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * int(seconds * 8000))


@pytest.mark.parametrize("seconds, expected", [(3, 1), (5.1, 2), (11, 3)])
async def test_recording_parts_are_transcribed_and_stitched(
    tmp_path, monkeypatch, seconds, expected
):
    path = tmp_path / "source.wav"
    make_audio(path, seconds)
    assignment = FeatureAssignment(connection="c", model="model", job_limit_s=5)
    calls = []

    async def transcribe(*args, **kwargs):
        part = args[3]
        calls.append((part.name, await audio_parts.duration(part), kwargs))
        return {
            "language": "en",
            "duration": 1,
            "segments": [{"start": 0.0, "end": 0.5, "speaker": "Speaker 1", "text": "hello"}],
            "speakers": [{"label": "Speaker 1", "embeddings": []}],
        }, {"model": "model", "audio_seconds": 1}

    context = SimpleNamespace(
        store=object(),
        lock=asyncio.Lock(),
        provider=SimpleNamespace(transcribe=transcribe),
        connection=lambda _: SimpleNamespace(id="c"),
        key=lambda _: "key",
        write=lambda *args: None,
    )
    worker = Recordings(context)
    monkeypatch.setattr(worker, "assignment", lambda: assignment)
    monkeypatch.setattr(worker, "save", lambda job: None)
    monkeypatch.setattr(worker, "calendar_speaker_hint", AsyncMock(return_value=None))
    monkeypatch.setattr(
        artifacts, "current_file", lambda *args: (path, {"size_bytes": path.stat().st_size})
    )
    monkeypatch.setattr(VoiceStore, "recognition_enabled", lambda self: False)
    job = TranscriptionJob(
        id="abc",
        artifact_id="a",
        filename="source.wav",
        mime_type="audio/wav",
        size_bytes=path.stat().st_size,
        title="test",
        provider="p",
        external_source_id="e",
        created_at="now",
    )
    result = await worker.transcribe(job)
    assert len(calls) == expected
    assert all(call[1] <= 5 for call in calls)
    assert all(not call[2]["include_speaker_embeddings"] for call in calls)
    assert [s["start"] for s in result["segments"]] == [i * 4 for i in range(expected)]
    assert [s["speaker"] for s in result["segments"]] == (
        ["Speaker 1"] if expected == 1 else [f"Part {i + 1}: Speaker 1" for i in range(expected)]
    )
    assert job.transcribed
    assert job.usage["audio_seconds"] == expected


async def test_part_error_names_part(tmp_path, monkeypatch):
    # A failed later part must not produce a partial transcript.
    path = tmp_path / "source.wav"
    make_audio(path, 6)
    context = SimpleNamespace(
        store=object(),
        lock=asyncio.Lock(),
        connection=lambda _: SimpleNamespace(id="c"),
        key=lambda _: "key",
        write=lambda *args: None,
    )
    context.provider = SimpleNamespace(
        transcribe=AsyncMock(
            side_effect=[
                ({"segments": [], "language": "en"}, {}),
                ProviderError("unavailable", "transcription-internal"),
            ]
        )
    )
    worker = Recordings(context)
    monkeypatch.setattr(
        worker, "assignment", lambda: FeatureAssignment(connection="c", job_limit_s=5)
    )
    monkeypatch.setattr(worker, "calendar_speaker_hint", AsyncMock(return_value=None))
    monkeypatch.setattr(
        artifacts, "current_file", lambda *args: (path, {"size_bytes": path.stat().st_size})
    )
    monkeypatch.setattr(VoiceStore, "recognition_enabled", lambda self: False)
    job = TranscriptionJob(
        id="abc",
        artifact_id="a",
        filename="source.wav",
        mime_type="audio/wav",
        size_bytes=path.stat().st_size,
        title="test",
        provider="p",
        external_source_id="e",
        created_at="now",
    )
    with pytest.raises(ProviderError, match="Part 2 of 2 failed"):
        await worker.transcribe(job)
    assert not job.transcribed


def speech():
    return {
        "language": "en",
        "duration": 1,
        "segments": [{"start": 0.0, "end": 0.5, "speaker": "Speaker 1", "text": "hello"}],
        "speakers": [],
    }, {}


def silence():
    return ProviderError("no speech", "transcript-empty")


async def transcribe_parts(tmp_path, monkeypatch, seconds, outcomes):
    path = tmp_path / "source.wav"
    make_audio(path, seconds)
    context = SimpleNamespace(
        store=object(),
        lock=asyncio.Lock(),
        connection=lambda _: SimpleNamespace(id="c"),
        key=lambda _: "key",
        write=lambda *args: None,
        provider=SimpleNamespace(transcribe=AsyncMock(side_effect=outcomes)),
    )
    worker = Recordings(context)
    monkeypatch.setattr(
        worker, "assignment", lambda: FeatureAssignment(connection="c", job_limit_s=5)
    )
    monkeypatch.setattr(worker, "save", lambda job: None)
    monkeypatch.setattr(worker, "calendar_speaker_hint", AsyncMock(return_value=None))
    monkeypatch.setattr(
        artifacts, "current_file", lambda *args: (path, {"size_bytes": path.stat().st_size})
    )
    monkeypatch.setattr(VoiceStore, "recognition_enabled", lambda self: False)
    job = TranscriptionJob(
        id="abc",
        artifact_id="a",
        filename="source.wav",
        mime_type="audio/wav",
        size_bytes=path.stat().st_size,
        title="test",
        provider="p",
        external_source_id="e",
        created_at="now",
    )
    return worker, job, context.provider.transcribe


async def test_a_silent_tail_part_is_skipped(tmp_path, monkeypatch):
    # 9 s with a 5 s limit is parts of 4, 4 and 1 s.
    worker, job, transcribe = await transcribe_parts(
        tmp_path, monkeypatch, 9, [speech(), speech(), silence()]
    )
    result = await worker.transcribe(job)
    assert transcribe.await_count == 3
    assert [s["start"] for s in result["segments"]] == [0, 4]
    assert [s["speaker"] for s in result["segments"]] == ["Part 1: Speaker 1", "Part 2: Speaker 1"]
    assert job.transcribed


async def test_a_silent_part_does_not_shift_later_parts(tmp_path, monkeypatch):
    worker, job, _ = await transcribe_parts(
        tmp_path, monkeypatch, 9, [silence(), speech(), speech()]
    )
    result = await worker.transcribe(job)
    assert [s["start"] for s in result["segments"]] == [4, 8]
    assert [s["speaker"] for s in result["segments"]] == ["Part 2: Speaker 1", "Part 3: Speaker 1"]


@pytest.mark.parametrize("rule", ["transcript-empty", "transcription-no_speech"])
async def test_a_recording_silent_in_every_part_fails_as_a_silent_one_does(
    tmp_path, monkeypatch, rule
):
    worker, job, transcribe = await transcribe_parts(
        tmp_path, monkeypatch, 9, [ProviderError("no speech", rule)] * 3
    )
    with pytest.raises(ProviderError) as caught:
        await worker.transcribe(job)
    assert caught.value.rule == rule and not str(caught.value).startswith("Part ")
    assert transcribe.await_count == 3
    assert not job.transcribed


async def test_speech_without_speaker_labels_fails_the_part(tmp_path, monkeypatch):
    worker, job, transcribe = await transcribe_parts(
        tmp_path,
        monkeypatch,
        9,
        [speech(), ProviderError("speaker labels missing", "transcript-no-speakers"), speech()],
    )
    with pytest.raises(ProviderError, match="Part 2 of 3 failed") as caught:
        await worker.transcribe(job)
    assert caught.value.rule == "transcript-no-speakers"
    assert transcribe.await_count == 2
    assert not job.transcribed


async def test_compressed_stereo_part_retains_codec_and_size(tmp_path):
    source = tmp_path / "source.mp3"
    await audio_parts._run(
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "anoisesrc=d=20:r=44100",
        "-ac",
        "2",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        "-y",
        str(source),
    )
    part, mime = await audio_parts.cut(source, tmp_path / "part-1", 4, 8, source.name)
    assert mime == "audio/mpeg" and part.suffix == ".mp3"
    assert part.stat().st_size < source.stat().st_size * 0.5
    assert 7.9 <= await audio_parts.duration(part) < 9
    probe = await audio_parts._run(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,channels,sample_rate",
        "-of",
        "csv=p=0",
        str(part),
    )
    assert probe.strip() == b"mp3,44100,2"


async def test_unsupported_copy_container_falls_back_to_flac(tmp_path):
    source = tmp_path / "source.wav"
    make_audio(source, 6)
    part, mime = await audio_parts.cut(source, tmp_path / "part", 0, 4, "source.unknown")
    assert mime == "audio/flac"
    assert part.suffix == ".flac"
    assert await audio_parts.duration(part) == pytest.approx(4)
