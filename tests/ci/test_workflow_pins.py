"""The supply chain rules for CI, enforced rather than remembered.

Three rules live here, and each is asserted against the workflow parsed into
a normalized model rather than against its text, so a spelling the parser
accepts cannot slip past a string match:

1. Every `uses:` is pinned to a full 40 character commit SHA. A tag is
   whatever it points at today.
2. No job queues on the self hosted `lab` pool. That pool exists to reach the
   lab and nothing here needs to.
3. No job is granted a token that could publish. Publication arrives with the
   release pull request, not before.

"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

WORKFLOWS = sorted((Path(__file__).resolve().parents[2] / ".github/workflows").glob("*.yml"))
PINNED = re.compile(r"^[^@]+@[0-9a-f]{40}$")

# Permissions a run of CI must not hold in this slice, and why.
FORBIDDEN_PERMISSIONS = {
    "packages": "could push an image",
    "id-token": "could mint a signing identity",
}


def load(workflow: Path) -> dict[str, Any]:
    return YAML(typ="safe").load(workflow.read_text(encoding="utf-8"))


def jobs(workflow: Path) -> dict[str, dict[str, Any]]:
    return dict(load(workflow).get("jobs") or {})


def runner_labels(job: dict[str, Any]) -> set[str]:
    """Every label a job asks for, whatever spelling `runs-on` was written in."""
    runs_on = job.get("runs-on")
    if isinstance(runs_on, str):
        return {runs_on}
    if isinstance(runs_on, list):
        return {str(label) for label in runs_on}
    if isinstance(runs_on, dict):
        labels = runs_on.get("labels", [])
        if isinstance(labels, str):
            labels = [labels]
        group = runs_on.get("group")
        return {str(label) for label in labels} | ({str(group)} if group else set())
    return set()


def granted(workflow: dict[str, Any], job: dict[str, Any], scope: str) -> str:
    """The access a job ends up with for one permission scope.

    A job level `permissions` replaces the workflow level one rather than
    merging with it, and the `write-all` and `read-all` shorthands stand for
    every scope at once.
    """
    permissions = job.get("permissions", workflow.get("permissions"))
    if permissions == "write-all":
        return "write"
    if isinstance(permissions, dict):
        return str(permissions.get(scope, "none"))
    return "none"


def test_there_is_at_least_one_workflow():
    assert WORKFLOWS, "no workflow files found; this test would pass vacuously"


def test_every_action_is_pinned_to_a_commit_sha():
    problems: list[str] = []
    for workflow in WORKFLOWS:
        for name, job in jobs(workflow).items():
            for step in job.get("steps") or []:
                ref = step.get("uses")
                if not ref or ref.startswith("./") or ref.startswith("docker://"):
                    continue
                if not PINNED.match(ref):
                    problems.append(f"{workflow.name}:{name}: {ref} is not pinned to a 40 hex SHA")
    assert not problems, "\n".join(problems)


def test_nothing_in_this_repository_queues_on_the_lab_runner_pool():
    """The lab pool exists to reach the lab. Nothing here needs to."""
    problems: list[str] = []
    for workflow in WORKFLOWS:
        for name, job in jobs(workflow).items():
            if "lab" in runner_labels(job):
                problems.append(f"{workflow.name}:{name} queues on the lab pool")
    assert not problems, "\n".join(problems)


def test_no_job_can_publish_an_image_in_this_slice():
    """Until the release pull request, no run of CI may hold a token that pushes."""
    problems: list[str] = []
    for workflow in WORKFLOWS:
        document = load(workflow)
        for name, job in (document.get("jobs") or {}).items():
            for scope, why in FORBIDDEN_PERMISSIONS.items():
                if granted(document, job, scope) == "write":
                    problems.append(f"{workflow.name}:{name} has {scope}: write and so {why}")
    assert not problems, "\n".join(problems)
