"""The notes filesystem as a Git repository.

The repository may be one a person already had, so every command runs with the
helper's own guard rails instead of trusting that repository's configuration:

- only the command line and the environment configure Git; global and system
  files are not read, so the helper behaves the same on every host;
- `safe.directory` names this work tree, so a restored volume with a different
  owner still works;
- hooks and the filesystem monitor are off and signing is never attempted, so
  nothing a restored `.git` carries is executed and no key is ever needed;
- discovery stops at the work tree, so a missing `.git` is never mistaken for a
  repository further up.

Nothing here pushes, fetches or names a remote.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from coppermind_git.settings import HelperSettings

EXCLUDE_BEGIN = "# BEGIN coppermind-git: rewritten by the helper, keep your own rules outside"
EXCLUDE_END = "# END coppermind-git"
# The store writes a note as `.<name>.<random>.tmp` beside it, then renames it
# into place. A scan must never record one of those.
TEMP_FILES = ".*.tmp"
# Left behind only when Git is killed mid-command, and any one of them stops
# every later commit.
STALE_LOCKS = ("index.lock", "HEAD.lock", "packed-refs.lock")
LISTED_CHANGES = 50


class GitError(RuntimeError):
    """A Git command failed or the work tree is not usable."""


@dataclass(frozen=True)
class Snapshot:
    """What is staged: the tree a commit would record and how it differs from HEAD."""

    tree: str
    changes: list[tuple[str, str]]


def kept_out(settings: HelperSettings) -> list[str]:
    """Top-level folders that never enter history.

    Obsidian's device state and its local trash, the generated source
    projections (rebuilt from source bundles, which live outside the notes
    filesystem), and deleted notes. Control state and credentials live under
    `/data/state`, outside the work tree, so Git never sees them at all.
    """
    return [
        ".obsidian",
        ".trash",
        settings.sources_folder.strip("/"),
        settings.trash_folder.strip("/"),
    ]


def kept_out_pathspec(settings: HelperSettings) -> list[str]:
    literal = [f":(literal){name}" for name in kept_out(settings)]
    return [*literal, f":(glob)**/{TEMP_FILES}"]


def exclude_block(settings: HelperSettings) -> str:
    rules = [f"/{_escape(name)}/" for name in kept_out(settings)]
    return "\n".join([EXCLUDE_BEGIN, *rules, TEMP_FILES, EXCLUDE_END])


def merge_block(existing: str, block: str) -> str:
    """Put `block` at the end of `existing`, replacing the previous copy of it.

    Every line outside the managed block stays exactly where it was.
    """
    lines = existing.splitlines()
    if EXCLUDE_BEGIN in lines:
        start = len(lines) - 1 - lines[::-1].index(EXCLUDE_BEGIN)
        if EXCLUDE_END in lines[start:]:
            end = lines.index(EXCLUDE_END, start)
            lines = lines[:start] + lines[end + 1 :]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join([*lines, "", block] if lines else [block]) + "\n"


def commit_message(changes: Sequence[tuple[str, str]]) -> str:
    count = len(changes)
    lines = [f"Coppermind snapshot: {count} file{'' if count == 1 else 's'}", ""]
    lines += [f"  {status}  {path}" for status, path in changes[:LISTED_CHANGES]]
    if count > LISTED_CHANGES:
        lines.append(f"  and {count - LISTED_CHANGES} more")
    return "\n".join(lines) + "\n"


def write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _escape(name: str) -> str:
    return re.sub(r"([\\*?\[\]!# ])", r"\\\1", name)


class NotesRepo:
    def __init__(self, work_tree: Path) -> None:
        self.work_tree = work_tree.absolute()
        self._opened = False

    def git(
        self,
        *args: str,
        config: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        environment.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CEILING_DIRECTORIES=str(self.work_tree.parent),
            GIT_TERMINAL_PROMPT="0",
        )
        environment.update(env or {})
        guard = [
            f"safe.directory={self.work_tree.resolve()}",
            "core.hooksPath=/dev/null",
            "core.fsmonitor=false",
            "core.quotePath=false",
            "commit.gpgSign=false",
            *config,
        ]
        command = ["git", *(part for item in guard for part in ("-c", item)), *args]
        result = subprocess.run(
            command,
            cwd=self.work_tree,
            env=environment,
            input=stdin,
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
        )
        if check and result.returncode != 0:
            detail = " ".join((result.stderr or result.stdout).split())
            raise GitError(f"git {args[0]} failed: {detail or f'exit {result.returncode}'}")
        return result

    def ensure(self, settings: HelperSettings) -> list[str]:
        """Make the work tree a repository, keeping one that is already there.

        Returns what it had to do, for the log. A `.git` that Git cannot open
        is reported and left alone, never initialised over.
        """
        if not self.work_tree.is_dir():
            raise GitError(f"the notes filesystem {self.work_tree} does not exist")
        done: list[str] = []
        if not (self.work_tree / ".git").exists():
            self.git("init", "--quiet", "--initial-branch=main")
            done.append("initialised a new repository")
        top = Path(self.git("rev-parse", "--show-toplevel").stdout.strip())
        if top.resolve() != self.work_tree.resolve():
            raise GitError(f"{self.work_tree} is not the top of its repository ({top} is)")
        git_dir = Path(self.git("rev-parse", "--absolute-git-dir").stdout.strip())

        # The helper is the only thing that runs Git here, and it has not run
        # yet in this process, so a lock can only be left over from a kill.
        if not self._opened:
            locks = [git_dir / name for name in STALE_LOCKS]
            locks += sorted((git_dir / "refs").rglob("*.lock"))
            for lock in locks:
                if lock.is_file():
                    lock.unlink()
                    done.append(f"removed a stale {lock.relative_to(git_dir)}")
            self._opened = True

        exclude = git_dir / "info" / "exclude"
        current = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        wanted = merge_block(current, exclude_block(settings))
        if wanted != current:
            write_atomically(exclude, wanted)
        return done

    def stage(self, settings: HelperSettings) -> Snapshot:
        """Stage every change, then take the excluded paths back out of the index.

        `info/exclude` keeps them from being added at all. The second step is
        what makes that a guarantee: a `!` rule in a person's own `.gitignore`
        outranks `info/exclude`, and a repository that already tracked
        `.obsidian/` stops tracking it here, while its files stay on disk and
        in earlier history. Naming the paths as exclusions to `git add` instead
        fails the whole command once any of them is also ignored.
        """
        self.git("add", "--all")
        self.git(
            "rm",
            "-r",
            "--cached",
            "--force",
            "--quiet",
            "--ignore-unmatch",
            "--",
            *kept_out_pathspec(settings),
        )
        tree = self.git("write-tree").stdout.strip()
        fields = self.git("diff", "--cached", "--name-status", "--no-renames", "-z").stdout
        parts = fields.split("\0")
        changes = [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]
        return Snapshot(tree=tree, changes=changes)

    def commit(self, settings: HelperSettings, snapshot: Snapshot) -> str:
        # Environment identity outranks any `user.*` or `author.*` a restored
        # repository configures, so every snapshot carries the settings' name.
        identity = {
            "GIT_AUTHOR_NAME": settings.identity_name,
            "GIT_AUTHOR_EMAIL": settings.identity_email,
            "GIT_COMMITTER_NAME": settings.identity_name,
            "GIT_COMMITTER_EMAIL": settings.identity_email,
        }
        # Housekeeping runs in the foreground so a container stop never
        # orphans it; `git.gc_auto: false` turns it off.
        config = ["gc.autoDetach=false", "maintenance.autoDetach=false"]
        if not settings.gc_auto:
            config += ["gc.auto=0", "maintenance.auto=false"]
        self.git(
            "commit",
            "--quiet",
            "--no-verify",
            "--cleanup=whitespace",
            "--file=-",
            config=config,
            env=identity,
            stdin=commit_message(snapshot.changes),
        )
        return self.git("rev-parse", "HEAD").stdout.strip()

    def last_commit(self) -> tuple[str, str] | None:
        """The sha and committer time of HEAD, or None before the first commit."""
        result = self.git("log", "-1", "--format=%H %cI", check=False)
        fields = result.stdout.split()
        return (fields[0], fields[1]) if result.returncode == 0 and len(fields) == 2 else None
