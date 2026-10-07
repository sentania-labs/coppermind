"""The make targets that drive CI, verified rather than remembered.

Rule: ``make check`` must depend on ``make check-local`` and
``compose-check`` (and ``test-node``) so the two lists can never drift.
This test parses the Makefile's internal database and asserts the
dependency structure.

CI calls ``make check`` and the worker calls ``make check-local``; the
dependency must exist so the lists stay in sync.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_deps(target: str) -> set[str]:
    """Parse ``make -p`` and return the direct prerequisites of *target*."""
    result = subprocess.run(
        ["make", "-p"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        # ``make -p`` renders a rule as:
        #   target: prereq1 prereq2 ...
        if line.startswith(f"{target}: "):
            return set(line.split(":", 1)[1].strip().split())
    return set()


def test_check_depends_on_check_local_and_compose_check():
    """make check must depend on check-local and compose-check so CI and workers
    share the same list of checks."""
    deps = _make_deps("check")
    assert "check-local" in deps, f"make check must depend on check-local; found: {deps}"
    assert "compose-check" in deps, f"make check must depend on compose-check; found: {deps}"
    assert "test-node" in deps, f"make check must depend on test-node; found: {deps}"


def test_check_local_is_worker_safe():
    """check-local must not invoke docker, COMPOSE, or npm directly so it runs
    without Docker CLI or Node present."""
    result = subprocess.run(
        ["make", "-n", "check-local"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    dry_run = result.stdout.lower()
    assert "docker" not in dry_run, "check-local must not invoke Docker; dry-run:\n" + result.stdout
    assert "npm" not in dry_run, "check-local must not invoke Node/npm; dry-run:\n" + result.stdout


def test_check_local_contains_expected_steps():
    """check-local must eventually run: setup, lint, typecheck, test,
    prose-check, scan-deps, scan-secrets."""
    # Use ``make -n`` dry-run to confirm the commands appear.
    result = subprocess.run(
        ["make", "-n", "check-local"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    dry_run = result.stdout

    expected = {
        "uv sync --all-packages",
        "ruff check .",
        "ruff format --check",
        "mypy coppermind",
        "pytest -q",
        "ci/prose-check.sh",
        "pip-audit",
        "gitleaks",
    }
    for keyword in expected:
        assert keyword in dry_run, (
            f"check-local must include a command containing {keyword}; dry-run:\n{dry_run}"
        )
