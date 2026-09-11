"""Git against real repositories on disk: what enters history and what never does."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from coppermind_git.repo import (
    EXCLUDE_BEGIN,
    GitError,
    NotesRepo,
    exclude_block,
    merge_block,
)
from coppermind_git.settings import HelperSettings

DEFAULTS = HelperSettings()
RunGit = Callable[..., str]


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def tracked(run_git: RunGit, notes: Path) -> set[str]:
    return set(run_git(notes, "ls-tree", "-r", "--name-only", "HEAD").splitlines())


def test_a_fresh_notes_filesystem_becomes_a_repository_with_the_note_in_it(
    tmp_path: Path, run_git: RunGit
):
    notes = tmp_path / "notes"
    write(notes / "Review" / "2026-09-08 Sync.md", "# Sync\n\n- target agreed\n")
    repo = NotesRepo(notes)

    assert repo.ensure(DEFAULTS) == ["initialised a new repository"]
    snapshot = repo.stage(DEFAULTS)
    assert snapshot.changes == [("A", "Review/2026-09-08 Sync.md")]
    sha = repo.commit(DEFAULTS, snapshot)

    shown = run_git(notes, "show", "--format=%an <%ae>%n%s", sha)
    assert shown.startswith("Coppermind <coppermind@localhost>\nCoppermind snapshot: 1 file\n")
    assert "+- target agreed" in shown
    assert run_git(notes, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert repo.ensure(DEFAULTS) == []


def test_an_existing_repository_keeps_its_history_branch_and_ignore_rules(
    tmp_path: Path, run_git: RunGit
):
    notes = tmp_path / "notes"
    notes.mkdir()
    run_git(notes, "init", "--quiet", "--initial-branch=trunk")
    write(notes / ".gitignore", "# mine\n*.private\n")
    write(notes / "note.md", "before\n")
    run_git(notes, "add", "--all")
    run_git(notes, "commit", "--quiet", "--message", "by hand")
    root = run_git(notes, "rev-parse", "HEAD").strip()
    ignore_before = (notes / ".gitignore").read_bytes()

    write(notes / "note.md", "after\n")
    write(notes / "diary.private", "not for history\n")
    repo = NotesRepo(notes)
    assert repo.ensure(DEFAULTS) == []
    snapshot = repo.stage(DEFAULTS)
    assert snapshot.changes == [("M", "note.md")]
    repo.commit(DEFAULTS, snapshot)

    assert run_git(notes, "rev-parse", "HEAD~1").strip() == root
    assert run_git(notes, "symbolic-ref", "--short", "HEAD").strip() == "trunk"
    assert (notes / ".gitignore").read_bytes() == ignore_before
    assert tracked(run_git, notes) == {".gitignore", "note.md"}


def test_device_state_trash_projections_and_temp_files_never_enter_history(
    tmp_path: Path, run_git: RunGit
):
    notes = tmp_path / "notes"
    # A person's own rules try to pull the excluded folders back in.
    write(notes / ".gitignore", "!/.obsidian/\n!_Trash/\n!*.tmp\n")
    for path in (
        ".obsidian/workspace.json",
        ".obsidian/app.json",
        ".trash/old.md",
        "Deleted/gone.md",
        "Generated/Plaud/2026-09-08 Call.md",
        "Review/.Sync.md.x1y2.tmp",
        ".Top.md.x1y2.tmp",
    ):
        write(notes / path, "excluded\n")
    for path in ("Review/Sync.md", "Deleted.md", "_Trash/renamed away, so a note now.md"):
        write(notes / path, "kept\n")
    settings = replace(DEFAULTS, sources_folder="Generated", trash_folder="Deleted")

    repo = NotesRepo(notes)
    repo.ensure(settings)
    repo.commit(settings, repo.stage(settings))

    assert tracked(run_git, notes) == {
        ".gitignore",
        "Review/Sync.md",
        "Deleted.md",
        "_Trash/renamed away, so a note now.md",
    }
    exclude = (notes / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "/Generated/\n/Deleted/\n" in exclude


def test_a_repository_that_tracked_device_state_stops_tracking_it_and_keeps_the_files(
    tmp_path: Path, run_git: RunGit
):
    notes = tmp_path / "notes"
    write(notes / ".obsidian" / "app.json", "{}\n")
    write(notes / "note.md", "text\n")
    run_git(notes, "init", "--quiet")
    run_git(notes, "add", "--all")
    run_git(notes, "commit", "--quiet", "--message", "by hand, with device state")

    repo = NotesRepo(notes)
    repo.ensure(DEFAULTS)
    snapshot = repo.stage(DEFAULTS)
    assert snapshot.changes == [("D", ".obsidian/app.json")]
    repo.commit(DEFAULTS, snapshot)

    assert tracked(run_git, notes) == {"note.md"}
    assert (notes / ".obsidian" / "app.json").is_file()
    assert "by hand, with device state" in run_git(notes, "log", "--format=%s")


def test_the_managed_exclude_block_moves_nothing_else():
    mine = "# mine\n*.bak\n"
    block = exclude_block(DEFAULTS)
    once = merge_block(mine, block)
    assert once.startswith(mine)
    assert merge_block(once, block) == once

    renamed = merge_block(once + "extra\n", exclude_block(replace(DEFAULTS, trash_folder="Bin")))
    assert renamed.startswith(mine)
    assert "extra\n" in renamed
    assert "/Bin/" in renamed
    assert "/_Trash/" not in renamed
    assert renamed.count(EXCLUDE_BEGIN) == 1


def test_a_restored_repository_cannot_block_redirect_or_run_hooks_in_a_snapshot(
    tmp_path: Path, run_git: RunGit
):
    notes = tmp_path / "notes"
    notes.mkdir()
    run_git(notes, "init", "--quiet")
    run_git(notes, "config", "commit.gpgSign", "true")
    run_git(notes, "config", "author.name", "Someone Else")
    hook = notes / ".git" / "hooks" / "post-commit"
    hook.write_text(f"#!/bin/sh\ntouch {notes / 'hook-ran'}\n", encoding="utf-8")
    hook.chmod(0o755)
    (notes / ".git" / "index.lock").write_text("", encoding="utf-8")
    write(notes / "note.md", "text\n")

    repo = NotesRepo(notes)
    assert repo.ensure(DEFAULTS) == ["removed a stale index.lock"]
    repo.commit(DEFAULTS, repo.stage(DEFAULTS))

    assert run_git(notes, "log", "-1", "--format=%an").strip() == "Coppermind"
    assert not (notes / "hook-ran").exists()


def test_a_missing_notes_filesystem_is_an_error(tmp_path: Path):
    with pytest.raises(GitError, match="does not exist"):
        NotesRepo(tmp_path / "notes").ensure(DEFAULTS)


def test_a_damaged_repository_is_reported_and_left_alone(tmp_path: Path):
    notes = tmp_path / "notes"
    (notes / ".git").mkdir(parents=True)
    write(notes / "note.md", "text\n")
    with pytest.raises(GitError):
        NotesRepo(notes).ensure(DEFAULTS)
    assert list((notes / ".git").iterdir()) == []
