from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "ci/check-release-tag.sh"


def git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=directory, check=True, text=True, capture_output=True
    ).stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, str]:
    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", "-b", "main", str(checkout))
    git(checkout, "config", "user.name", "Release Test")
    git(checkout, "config", "user.email", "release@example.invalid")
    (checkout / "proof").write_text("tested\n", encoding="utf-8")
    git(checkout, "add", "proof")
    git(checkout, "commit", "-m", "tested")
    git(checkout, "remote", "add", "origin", str(remote))
    git(checkout, "push", "-u", "origin", "main")
    git(checkout, "tag", "-a", "v1.2.3", "-m", "v1.2.3")
    git(checkout, "push", "origin", "v1.2.3")
    return checkout, git(checkout, "rev-parse", "HEAD")


def check(checkout: Path, sha: str, version: str) -> subprocess.CompletedProcess[str]:
    env = os.environ | {
        "VERSION": version,
        "GITHUB_SHA": sha,
        "MAINLINE_REF": "main",
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=checkout, env=env, text=True, capture_output=True
    )


def test_an_annotated_version_on_main_is_accepted(tmp_path: Path):
    checkout, sha = repository(tmp_path)
    result = check(checkout, sha, "v1.2.3")
    assert result.returncode == 0, result.stderr


def test_a_non_version_tag_is_refused(tmp_path: Path):
    checkout, sha = repository(tmp_path)
    result = check(checkout, sha, "v1.2")
    assert result.returncode == 2
    assert "vMAJOR.MINOR.PATCH" in result.stderr


def test_a_lightweight_tag_is_refused(tmp_path: Path):
    checkout, sha = repository(tmp_path)
    git(checkout, "tag", "v2.0.0")
    git(checkout, "push", "origin", "v2.0.0")
    result = check(checkout, sha, "v2.0.0")
    assert result.returncode == 2
    assert "annotated" in result.stderr


def test_a_version_tag_from_an_unmerged_branch_is_refused(tmp_path: Path):
    checkout, _ = repository(tmp_path)
    git(checkout, "checkout", "-b", "stray")
    (checkout / "proof").write_text("not merged\n", encoding="utf-8")
    git(checkout, "commit", "-am", "stray")
    sha = git(checkout, "rev-parse", "HEAD")
    git(checkout, "tag", "-a", "v2.0.0", "-m", "v2.0.0")
    git(checkout, "push", "origin", "v2.0.0")
    result = check(checkout, sha, "v2.0.0")
    assert result.returncode == 1
    assert "not reachable" in result.stderr
