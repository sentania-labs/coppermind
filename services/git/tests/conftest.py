from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def run_git() -> Callable[..., str]:
    """Git as a person at a terminal would run it, isolated from this host's config."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="A Person",
        GIT_AUTHOR_EMAIL="person@example.com",
        GIT_COMMITTER_NAME="A Person",
        GIT_COMMITTER_EMAIL="person@example.com",
    )

    def run(cwd: Path, *args: str) -> str:
        command = ["git", "-c", "core.quotePath=false", *args]
        return subprocess.run(
            command, cwd=cwd, env=env, check=True, capture_output=True, text=True
        ).stdout

    return run
