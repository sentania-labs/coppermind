"""The supply chain rules for CI, enforced rather than remembered.

Three rules live here, and each is asserted against the workflow parsed into
a normalized model rather than against its text, so a spelling the parser
accepts cannot slip past a string match:

1. Every `uses:` is pinned to a full 40 character commit SHA. A tag is
   whatever it points at today.
2. No job queues on the self hosted `lab` pool. That pool exists to reach the
   lab and nothing here needs to.
3. Publication authority belongs only to tag-triggered release jobs.

"""

from __future__ import annotations

import fnmatch
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

# The one condition that may carry publication authority. Anything else, including
# a disjunction that happens to name both halves, is true on pushes that are not
# a version tag.
TAG_GATE = "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/')"


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


def test_publish_authority_is_confined_to_tag_only_jobs():
    problems: list[str] = []
    for workflow in WORKFLOWS:
        document = load(workflow)
        for name, job in (document.get("jobs") or {}).items():
            for scope, why in FORBIDDEN_PERMISSIONS.items():
                if (
                    granted(document, job, scope) == "write"
                    and str(job.get("if", "")).strip() != TAG_GATE
                ):
                    problems.append(
                        f"{workflow.name}:{name} has {scope}: write and so {why} "
                        f"unless its condition is exactly {TAG_GATE}"
                    )
    assert not problems, "\n".join(problems)


def test_latest_promotion_serializes_across_tags():
    """The promotion guard is a read then write, so two tags must not overlap.

    `ci/promote-latest.sh` reads the version `latest` serves and refuses to go
    backwards, then copies this tag's digests over it. A concurrency group that
    varies by ref puts two tags' promotions in different queues, and the older
    one can land last.
    """
    workflow = load(WORKFLOWS[0])
    promote = workflow["jobs"]["promote-latest"]
    group = str(promote["concurrency"]["group"])
    assert "${{" not in group, (
        f"promote-latest queues in {group}, which varies by run, so two tags "
        "can promote at once and the older one can win"
    )
    assert promote["concurrency"]["cancel-in-progress"] is False


def test_release_waits_for_every_image_and_the_running_stack():
    workflow = load(WORKFLOWS[0])
    publish = workflow["jobs"]["publish"]
    assert set(publish["needs"]) == {
        "image",
        "smoke",
        "integration",
        "release-tag",
        "release-mode",
    }
    assert publish["runs-on"] == "ubuntu-latest"


def test_the_integration_gate_downloads_what_every_shard_uploads():
    """The shards and the aggregate run on separate machines.

    `integration` waits for every shard, and the only way it can see a shard's
    file list and pytest output is an artifact that shard uploaded, so each
    shard's artifact name must match what the aggregate job downloads.
    """
    all_jobs = jobs(WORKFLOWS[0])
    shard, gate = all_jobs["integration-shard"], all_jobs["integration"]
    assert gate["needs"] == "integration-shard"

    uploaded = {
        str(step["with"]["name"]).replace("${{ matrix.shard }}", str(number))
        for step in shard["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact")
        for number in shard["strategy"]["matrix"]["shard"]
    }
    pattern = next(
        str(step["with"]["pattern"])
        for step in gate["steps"]
        if step.get("uses", "").startswith("actions/download-artifact")
    )
    assert len(uploaded) == len(shard["strategy"]["matrix"]["shard"])
    assert all(fnmatch.fnmatchcase(name, pattern) for name in uploaded)


def test_dependency_review_runs_the_local_audit_without_a_licensed_api():
    job = jobs(WORKFLOWS[0])["dependency-review"]
    assert not job.get("continue-on-error")
    steps = job["steps"]
    assert any(step.get("run") == "make scan-deps" for step in steps)
    assert not any("dependency-review-action" in step.get("uses", "") for step in steps)
    assert not any(step.get("continue-on-error") for step in steps)
