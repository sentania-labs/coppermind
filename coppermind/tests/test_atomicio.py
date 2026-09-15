"""Durability of the two writers of `/data`.

Both paths promise the same thing: a failure leaves no half written file for
Obsidian Sync to carry to every device. These tests inject the failure that
matters in practice, a full volume, by making the fsync fail.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coppermind import atomicio
from coppermind.atomicio import (
    atomic_write_bytes,
    commit_staged,
    create_exclusive_bytes,
    stage_bytes,
)


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


def test_a_failed_directory_fsync_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The durability step is part of the write, so its failure undoes the write.

    The file is complete and closed by the time the directory is synced, so a
    volume that rejects the directory fsync used to leave a fully formed note
    behind while the caller was told the write did not succeed.
    """

    def boom(_: Path) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(atomicio, "_fsync_dir", boom)
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


def test_staged_bytes_are_durable_before_the_rename_and_gone_after_it(tmp_path: Path):
    """A caller can check the target between staging and the rename.

    The staged file is complete beside the target, the target is untouched
    until the rename, and nothing temporary is left in the directory after.
    """
    target = tmp_path / "Review" / "Runbook.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"# Old\n")
    staged = stage_bytes(target, b"# New\n")
    assert staged.parent == target.parent and staged.name.startswith(".Runbook.md.")
    assert staged.read_bytes() == b"# New\n"
    assert target.read_bytes() == b"# Old\n"
    commit_staged(staged, target)
    assert target.read_bytes() == b"# New\n"
    assert list(target.parent.iterdir()) == [target]


def test_a_failed_stage_leaves_no_temporary(tmp_path: Path, failing_fsync):
    target = tmp_path / "Review" / "Runbook.md"
    with pytest.raises(OSError):
        stage_bytes(target, b"# New\n")
    assert list(target.parent.iterdir()) == []
