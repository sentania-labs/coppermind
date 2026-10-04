from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SELF_PATH = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()
NAMES = ("AGENTS.md", "CLAUDE.md")


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, check=True, text=True, capture_output=True
    )
    return [line for line in result.stdout.splitlines() if line]


def test_agent_files_do_not_exist_at_the_repository_root():
    for name in NAMES:
        assert not (REPO_ROOT / name).exists(), f"{name} must not exist at the repository root"


def test_no_tracked_file_references_the_agent_files():
    offenders: list[str] = []
    for path in tracked_files():
        if path == SELF_PATH:
            continue
        full_path = REPO_ROOT / path
        if not full_path.is_file():
            continue
        try:
            text = full_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError):
            continue
        for name in NAMES:
            if name in text:
                offenders.append(f"{path}: {name}")
    assert not offenders, "tracked files still reference a removed agent file:\n" + "\n".join(
        offenders
    )
