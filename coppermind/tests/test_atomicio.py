"""Durability of the two writers of `/data`.

Both paths promise the same thing: a failure leaves no half written file for
Obsidian Sync to carry to every device. These tests inject the failure that
matters in practice, a full volume, by making the fsync fail.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coppermind.atomicio import atomic_write_bytes, create_exclusive_bytes


@pytest.fixture
def failing_fsync(monkeypatch: pytest.MonkeyPatch):
    def boom(_: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", boom)


def test_an_exclusive_create_writes_the_bytes_and_refuses_to_clobber(tmp_path: Path):
    target = tmp_path / "Review" / "Runbook.md"
    create_exclusive_bytes(target, b"# Runbook\n")
    assert target.read_bytes() == b"# Runbook\n"
    with pytest.raises(FileExistsError):
        create_exclusive_bytes(target, b"# Something else\n")
    assert target.read_bytes() == b"# Runbook\n"


def test_a_failed_exclusive_create_leaves_nothing_behind(tmp_path: Path, failing_fsync):
    """A full volume must not leave an empty note for Obsidian Sync to spread."""
    target = tmp_path / "Review" / "Runbook.md"
    with pytest.raises(OSError):
        create_exclusive_bytes(target, b"# Runbook\n")
    assert not target.exists()


def test_a_failed_atomic_write_leaves_neither_the_target_nor_a_temporary(
    tmp_path: Path, failing_fsync
):
    target = tmp_path / "state" / "settings.yaml"
    with pytest.raises(OSError):
        atomic_write_bytes(target, b"revision: 1\n")
    assert not target.exists()
    assert list(target.parent.iterdir()) == []
