from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SELF_PATH = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()
NAMES = ("AGENTS.md", "CLAUDE.md")


def tracked_files(root: Path) -> list[str]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True)
    return [entry.decode("utf-8") for entry in result.stdout.split(b"\0") if entry]


def find_offenders(root: Path, self_path: str | None = None) -> list[str]:
    offenders: list[str] = []
    for path in tracked_files(root):
        if self_path is not None and path == self_path:
            continue
        full_path = root / path
        if not full_path.is_file():
            continue
        try:
            text = full_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError):
            continue
        for name in NAMES:
            if name in text:
                offenders.append(f"{path}: {name}")
    return offenders


def test_agent_files_are_not_part_of_the_repository():
    tracked = set(tracked_files(REPO_ROOT))
    for name in NAMES:
        assert name not in tracked, f"{name} must not be tracked in the repository"
        full_path = REPO_ROOT / name
        if not full_path.exists():
            continue
        # A local harness may drop an untracked, git-ignored shim at this path
        # (see .git/info/exclude); that is environment setup, not repo content.
        ignored = subprocess.run(["git", "check-ignore", "-q", name], cwd=REPO_ROOT).returncode == 0
        assert ignored, f"{name} exists at the repository root and is not git-ignored"


def test_no_tracked_file_references_the_agent_files():
    offenders = find_offenders(REPO_ROOT, SELF_PATH)
    assert not offenders, "tracked files still reference a removed agent file:\n" + "\n".join(
        offenders
    )


def test_non_ascii_filename_referencing_agent_file_is_caught(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    offending_name = "café.md"
    (tmp_path / offending_name).write_text("See AGENTS.md for the rules.", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    offenders = find_offenders(tmp_path)

    assert any(offending_name in offender for offender in offenders), offenders
