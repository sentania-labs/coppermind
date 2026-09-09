"""The supply chain rule for CI, enforced rather than remembered.

A GitHub Action referenced by a tag is whatever the tag points at today. Every
`uses:` in this repository is pinned to a full 40 character commit SHA, with
the human readable version in a trailing comment so a reader can still tell
what it is. This test is the gate; without it the rule quietly rots.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = sorted((Path(__file__).resolve().parents[2] / ".github/workflows").glob("*.yml"))
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)\s*(?P<comment>#.*)?$")
PINNED = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def directives(workflow: Path) -> str:
    """The workflow with its comments removed.

    The checks below look for literal strings, and this file's own prose
    explains why those strings must not appear. Reading only the directives
    keeps a comment about a rule from tripping the rule.
    """
    lines = []
    for line in workflow.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].rstrip()
        if stripped:
            lines.append(stripped)
    return "\n".join(lines)


def test_there_is_at_least_one_workflow():
    assert WORKFLOWS, "no workflow files found; this test would pass vacuously"


def test_every_action_is_pinned_to_a_commit_sha_with_its_version_in_a_comment():
    problems: list[str] = []
    for workflow in WORKFLOWS:
        for number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), start=1):
            match = USES.match(line)
            if not match:
                continue
            ref = match.group("ref")
            if ref.startswith("./") or ref.startswith("docker://"):
                continue
            if not PINNED.match(ref):
                problems.append(f"{workflow.name}:{number}: {ref} is not pinned to a 40 hex SHA")
            elif not match.group("comment"):
                problems.append(f"{workflow.name}:{number}: {ref} has no version comment")
    assert not problems, "\n".join(problems)


def test_nothing_in_this_repository_queues_on_the_lab_runner_pool():
    """The lab pool exists to reach the lab. Nothing here needs to."""
    for workflow in WORKFLOWS:
        assert "runs-on: lab" not in directives(workflow), f"{workflow.name} queues on the lab pool"


def test_no_job_can_publish_an_image_in_this_slice():
    """Publication arrives with the release pull request, not before.

    Until then no run of CI may hold a token that could push, so nothing here
    asks for `packages: write` or `id-token: write`.
    """
    for workflow in WORKFLOWS:
        text = directives(workflow)
        assert "packages: write" not in text, f"{workflow.name} can push an image"
        assert "id-token: write" not in text, f"{workflow.name} can mint a signing identity"
