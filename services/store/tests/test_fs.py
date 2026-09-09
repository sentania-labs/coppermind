import os
from pathlib import Path

import pytest
from coppermind_store.fs import content_hash, existing_stems, is_writable, resolve


def test_the_etag_is_a_hash_of_the_exact_bytes():
    assert content_hash(b"abc").startswith("sha256:")
    assert content_hash(b"abc") == content_hash(b"abc")
    assert content_hash(b"abc") != content_hash(b"abd")


def test_a_path_cannot_escape_the_notes_filesystem(tmp_path: Path):
    root = tmp_path / "notes"
    root.mkdir()
    assert resolve(root, "Review/note.md") == root / "Review/note.md"
    with pytest.raises(ValueError):
        resolve(root, "../secrets")


def test_existing_stems_lists_what_a_collision_would_hit(tmp_path: Path):
    folder = tmp_path / "Review"
    folder.mkdir()
    (folder / "Notes.md").write_text("", encoding="utf-8")
    (folder / "Diagram.png").write_bytes(b"image")
    assert existing_stems(folder) == ["Notes"]
    assert existing_stems(tmp_path / "missing") == []


def test_a_readiness_check_leaves_every_existing_file_alone(tmp_path: Path):
    """The probe must never be a name a person could already be using.

    A readiness check runs on a schedule, so a fixed probe path would truncate
    and then delete whatever sits there, inside the one directory this service
    exists to protect.
    """
    root = tmp_path / "notes"
    root.mkdir()
    (root / ".coppermind-write-probe").write_text("someone's file", encoding="utf-8")
    (root / "Runbook.md").write_text("# Runbook\n", encoding="utf-8")
    before = {entry.name: entry.read_text(encoding="utf-8") for entry in root.iterdir()}

    assert is_writable(root) == (True, "")

    after = {entry.name: entry.read_text(encoding="utf-8") for entry in root.iterdir()}
    assert after == before


def test_readiness_reports_a_notes_filesystem_it_cannot_write(tmp_path: Path):
    if os.geteuid() == 0:
        pytest.skip("root ignores the directory mode this test relies on")
    root = tmp_path / "notes"
    root.mkdir(mode=0o500)
    ok, detail = is_writable(root)
    assert ok is False
    assert detail
    root.chmod(0o700)
    assert is_writable(root) == (True, "")
