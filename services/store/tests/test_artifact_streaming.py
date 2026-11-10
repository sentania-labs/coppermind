"""Archived artifacts are checked and served in bounded chunks, never read whole."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from coppermind_store import artifacts

DATA = bytes(range(256)) * (3 * 1024 * 1024 // 256) + b"tail"


@pytest.fixture
def archived(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "recording.m4a"
    path.write_bytes(DATA)

    def whole(self: Path) -> bytes:
        raise AssertionError(f"read whole: {self.name}")

    monkeypatch.setattr(Path, "read_bytes", whole)
    return path


async def test_a_file_is_served_in_chunks_no_larger_than_the_read_limit(archived):
    sizes: list[int] = []
    served = bytearray()
    async for chunk in artifacts._read_chunks(archived):
        sizes.append(len(chunk))
        served += chunk
    assert bytes(served) == DATA
    assert len(sizes) >= 4 and max(sizes) <= artifacts.READ_CHUNK_BYTES


def test_verification_and_hashing_pass_for_the_recorded_bytes(archived):
    sha = hashlib.sha256(DATA).hexdigest()
    artifacts._verify_file(archived, sha, len(DATA))
    assert artifacts._file_sha256(archived) == sha


@pytest.mark.parametrize("damage", ["bytes", "size"])
def test_verification_refuses_a_file_that_differs_from_the_record(archived, damage):
    sha = hashlib.sha256(DATA).hexdigest()
    size = len(DATA) + (1 if damage == "size" else 0)
    if damage == "bytes":
        sha = hashlib.sha256(DATA + b"x").hexdigest()
    with pytest.raises(artifacts.ArtifactsFilesystemUnavailable, match="verification"):
        artifacts._verify_file(archived, sha, size)
