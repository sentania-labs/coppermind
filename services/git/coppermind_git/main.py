"""The Git helper process: poll the notes filesystem, commit what settles.

It polls rather than watching for filesystem events, which is the mode the
design uses where events are unreliable, and it is the only mode here:

- A scan runs at start and then every `git.poll_interval_s`. A scan stages
  every change outside the excluded paths.
- When a scan finds changes, the next one comes `git.debounce_s` later and
  commits only if nothing changed in between, so a note still being typed
  lands as one snapshot rather than a dozen half-typed ones.
- Every 30 seconds, whether or not a scan is due, `settings.yaml` is re-read
  and `/data/state/git/status.json` is rewritten, so the file's age says
  whether the helper is alive.

Starting the helper therefore catches up on whatever changed while it was
stopped: the first scan finds it and the commit follows one debounce later.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

from coppermind_git import __version__
from coppermind_git.health import status_path
from coppermind_git.repo import GitError, NotesRepo, write_atomically
from coppermind_git.settings import HelperSettings, SettingsError, load

HEARTBEAT_S = 30.0

log = logging.getLogger("coppermind.git")


def utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


class Helper:
    def __init__(self, data_dir: Path) -> None:
        self.repo = NotesRepo(data_dir / "notes")
        self.settings_path = data_dir / "state" / "settings.yaml"
        self.status_path = status_path(data_dir)
        self.settings = HelperSettings()
        self.settings_error: str | None = None
        self.scan_error: str | None = None
        self.last_scan_at: float | None = None
        self.last_run_at: str | None = None
        self.pending_tree: str | None = None
        self.dirty_files = 0
        self.last_commit: tuple[str, str] | None = None

    def tick(self, now: float) -> float:
        """Do whatever is due at `now` and return the seconds until the next tick."""
        self._refresh_settings()
        if self.seconds_until_scan(now) <= 0:
            self._scan(now)
        self._write_status()
        return max(1.0, min(HEARTBEAT_S, self.seconds_until_scan(now)))

    def seconds_until_scan(self, now: float) -> float:
        if not self.settings.enabled:
            return HEARTBEAT_S
        if self.last_scan_at is None:
            return 0.0
        # A failed scan retries on the short interval, as a pending change does.
        settling = self.pending_tree is not None or self.scan_error is not None
        wait = self.settings.debounce_s if settling else self.settings.poll_interval_s
        return self.last_scan_at + wait - now

    def status(self) -> dict[str, Any]:
        sha, committed_at = self.last_commit or (None, None)
        return {
            "schema_version": 1,
            "enabled": self.settings.enabled,
            "last_run_at": self.last_run_at,
            "last_commit_at": committed_at,
            "last_commit_sha": sha,
            "dirty_files": self.dirty_files,
            "last_error": self.scan_error or self.settings_error,
            "watch_mode": "poll",
            "debounce_s": self.settings.debounce_s,
        }

    def _refresh_settings(self) -> None:
        try:
            loaded = load(self.settings_path)
        except SettingsError as exc:
            if self.settings_error != str(exc):
                emit(logging.ERROR, "settings not usable, keeping the last good ones", error=exc)
            self.settings_error = str(exc)
            return
        if loaded != self.settings:
            emit(logging.INFO, "settings applied", **vars(loaded))
        self.settings, self.settings_error = loaded, None

    def _scan(self, now: float) -> None:
        self.last_scan_at = now
        try:
            for action in self.repo.ensure(self.settings):
                emit(logging.WARNING, action, path=self.repo.work_tree)
            if self.last_commit is None:
                self.last_commit = self.repo.last_commit()
            snapshot = self.repo.stage(self.settings)
            if not snapshot.changes:
                self.pending_tree = None
            elif snapshot.tree == self.pending_tree:
                sha = self.repo.commit(self.settings, snapshot)
                self.last_commit = self.repo.last_commit()
                self.pending_tree = None
                emit(logging.INFO, "recorded a snapshot", sha=sha, files=len(snapshot.changes))
            else:
                self.pending_tree = snapshot.tree
            self.dirty_files = len(snapshot.changes) if self.pending_tree else 0
            self.last_run_at = utc(now)
            self.scan_error = None
        except (GitError, OSError) as exc:
            if self.scan_error != str(exc):
                emit(logging.ERROR, "scan failed", error=exc)
            self.scan_error = str(exc)

    def _write_status(self) -> None:
        try:
            write_atomically(self.status_path, json.dumps(self.status(), indent=2) + "\n")
        except OSError as exc:
            emit(logging.ERROR, "could not write the status file", error=exc)


def run(helper: Helper, stop: threading.Event) -> None:
    while not stop.is_set():
        stop.wait(helper.tick(time.time()))


class _JsonFormatter(logging.Formatter):
    """The field names the other services log with, without their dependencies."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": utc(record.created),
            "level": record.levelname.lower(),
            "service": "git",
            "event": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        return json.dumps(entry, default=str)


def emit(level: int, event: str, **fields: object) -> None:
    log.log(level, event, extra={"fields": fields})


def main() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    logging.basicConfig(handlers=[handler], force=True)
    requested = os.environ.get("COPPERMIND_LOG_LEVEL", "INFO").upper()
    log.setLevel(logging.getLevelNamesMapping().get(requested, logging.INFO))

    data_dir = Path(os.environ.get("COPPERMIND_DATA_DIR", "/data"))
    stop = threading.Event()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        emit(logging.INFO, "stopping", signal=signal.Signals(signum).name)
        stop.set()

    # PID 1 in a container ignores a signal it has no handler for, and a stop
    # between commands leaves no lock behind.
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    version = os.environ.get("COPPERMIND_BUILD_VERSION") or __version__
    emit(logging.INFO, "starting", version=version, data_dir=data_dir, watch_mode="poll")
    run(Helper(data_dir), stop)


if __name__ == "__main__":
    main()
