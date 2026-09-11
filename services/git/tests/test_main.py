"""The polling loop, driven with an explicit clock against a real repository."""

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from coppermind_git import health
from coppermind_git.main import HEARTBEAT_S, Helper

RunGit = Callable[..., str]


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    (tmp_path / "notes" / "Review").mkdir(parents=True)
    return tmp_path


def edit(data_dir: Path, name: str, text: str) -> None:
    (data_dir / "notes" / "Review" / name).write_text(text, encoding="utf-8")


def commits(run_git: RunGit, data_dir: Path) -> list[str]:
    return run_git(data_dir / "notes", "rev-list", "--all").split()


def status(data_dir: Path) -> dict:
    return json.loads(health.status_path(data_dir).read_text(encoding="utf-8"))


def test_a_change_is_recorded_once_it_has_been_quiet_for_the_debounce(
    data_dir: Path, run_git: RunGit
):
    edit(data_dir, "Sync.md", "first\n")
    helper = Helper(data_dir)

    assert helper.tick(0.0) == HEARTBEAT_S
    assert status(data_dir)["dirty_files"] == 1
    helper.tick(30.0)
    assert commits(run_git, data_dir) == []

    helper.tick(60.0)
    [sha] = commits(run_git, data_dir)
    recorded = status(data_dir)
    assert recorded["last_commit_sha"] == sha
    assert recorded["dirty_files"] == 0
    assert recorded["last_error"] is None
    assert recorded["watch_mode"] == "poll"
    assert recorded["debounce_s"] == 60


def test_an_edit_during_the_debounce_waits_for_the_next_quiet_spell(
    data_dir: Path, run_git: RunGit
):
    edit(data_dir, "Sync.md", "typing\n")
    helper = Helper(data_dir)
    helper.tick(0.0)
    edit(data_dir, "Sync.md", "typing more\n")
    helper.tick(60.0)
    assert commits(run_git, data_dir) == []

    helper.tick(120.0)
    notes = data_dir / "notes"
    assert run_git(notes, "show", "HEAD:Review/Sync.md") == "typing more\n"


def test_a_clean_tree_is_scanned_again_after_the_poll_interval(data_dir: Path, run_git: RunGit):
    helper = Helper(data_dir)
    helper.tick(0.0)
    edit(data_dir, "Later.md", "arrived after the scan\n")
    helper.tick(299.0)
    assert status(data_dir)["dirty_files"] == 0

    helper.tick(300.0)
    assert status(data_dir)["dirty_files"] == 1
    helper.tick(360.0)
    assert len(commits(run_git, data_dir)) == 1


def test_a_restart_keeps_history_and_catches_up_on_what_changed_while_stopped(
    data_dir: Path, run_git: RunGit
):
    edit(data_dir, "Sync.md", "one\n")
    first = Helper(data_dir)
    first.tick(0.0)
    first.tick(60.0)
    [root] = commits(run_git, data_dir)

    edit(data_dir, "Sync.md", "two, written while the helper was stopped\n")
    second = Helper(data_dir)
    second.tick(1000.0)
    assert status(data_dir)["last_commit_sha"] == root
    second.tick(1060.0)

    notes = data_dir / "notes"
    assert run_git(notes, "rev-parse", "HEAD~1").strip() == root
    assert "+two, written while the helper was stopped" in run_git(notes, "show", "HEAD")


def test_disabled_means_no_repository_and_bad_settings_keep_the_last_good(
    data_dir: Path, run_git: RunGit
):
    settings = data_dir / "state" / "settings.yaml"
    settings.parent.mkdir()
    settings.write_text("git:\n  enabled: false\n", encoding="utf-8")
    edit(data_dir, "Sync.md", "text\n")
    helper = Helper(data_dir)
    helper.tick(0.0)
    assert not (data_dir / "notes" / ".git").exists()
    assert status(data_dir)["enabled"] is False

    settings.write_text("git:\n  debounce_s: 0\n", encoding="utf-8")
    helper.tick(30.0)
    recorded = status(data_dir)
    assert recorded["enabled"] is False
    assert "git.debounce_s" in recorded["last_error"]


def test_health_follows_the_age_of_the_status_file(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COPPERMIND_DATA_DIR", str(data_dir))

    def exit_code() -> int | str | None:
        with pytest.raises(SystemExit) as exited:
            health.main()
        return exited.value.code

    assert exit_code() == 1
    Helper(data_dir).tick(0.0)
    assert exit_code() == 0
    stale = time.time() - health.MAX_AGE_S - 1
    os.utime(health.status_path(data_dir), (stale, stale))
    assert exit_code() == 1
