"""The two CI scripts that make decisions, verified against stubbed input.

`ci/release-mode.sh` is the tag build's fork in the road, so its answers are
asserted rather than remembered: minimal only when the main run passed every
gate, full when the lookup is empty, the run concluded otherwise, or a gate is
simply not there. `ci/shard-list.sh` is the partition itself, so its subsets are
checked against the whole list.

`ci/check-shard-coverage.sh` is the proof that the parallel shards together ran the
whole suite once, so it is executed here against real shard files for the real
suite.

The API is stubbed by the two variables `ci/release-mode.sh` reads before it
touches the network, so each test fixes the runs document and the job list and the
script keeps its own decision logic.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODE_SCRIPT = REPO_ROOT / "ci/release-mode.sh"
SHARD_SCRIPT = REPO_ROOT / "ci/shard-list.sh"

SHA = "0" * 40
COVERAGE_SCRIPT = REPO_ROOT / "ci/check-shard-coverage.sh"

# The job names the GitHub jobs API reports for a run of `.github/workflows/ci.yml`:
# a matrix job is reported once per entry, named `<job> (<entry>)`.
REPORTED = (
    "checks",
    "scan",
    "release-tag",
    "image (store)",
    "image (api)",
    "image (admin)",
    "image (git)",
    "image (obsidian-sync)",
    "smoke",
    "integration-shard (1)",
    "integration-shard (2)",
    "integration-shard (3)",
    "integration-shard (4)",
    "integration",
    "dependency-review",
)


def jobs_payload(*, skip: str | None = None, failing: str | None = None) -> dict[str, object]:
    return {
        "jobs": [
            {
                "name": name,
                "conclusion": "failure" if name == failing else "success",
            }
            for name in REPORTED
            if name != skip and not (skip and name.startswith(f"{skip} ("))
        ]
    }


def runs_payload(
    *, conclusion: str | None = "success", number: int = 42, branch: str = "main"
) -> dict[str, object]:
    if conclusion is None:
        return {"workflow_runs": []}
    return {
        "workflow_runs": [
            {
                "run_number": number,
                "head_sha": SHA,
                "head_branch": branch,
                "event": "push",
                "status": "completed",
                "conclusion": conclusion,
                "jobs_url": "http://127.0.0.1:9/jobs",
            }
        ]
    }


def decide(
    tmp_path: Path,
    runs: dict[str, object],
    jobs: dict[str, object] | None = None,
) -> dict[str, str]:
    """Run `ci/release-mode.sh` against stubbed answers and return its verdict."""
    env = os.environ | {
        "RELEASE_JSON": json.dumps(runs),
        "RELEASE_JOBS": json.dumps(jobs) if jobs is not None else "",
        "GITHUB_SHA": SHA,
        "GITHUB_REF_NAME": "v1.2.3",
        "GITHUB_REPOSITORY": "sentania-labs/coppermind",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }
    result = subprocess.run(
        ["bash", str(MODE_SCRIPT), str(tmp_path / "outputs")],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in result.stdout.strip().splitlines())


def test_the_main_run_having_passed_makes_the_tag_minimal(tmp_path: Path):
    answer = decide(tmp_path, runs_payload(), jobs_payload())
    assert answer["mode"] == "minimal"
    assert "42" in answer["reason"]


def test_a_failed_or_cancelled_main_run_keeps_the_tag_full(tmp_path: Path):
    for conclusion in ("failure", "cancelled"):
        answer = decide(tmp_path, runs_payload(conclusion=conclusion), jobs_payload())
        assert answer["mode"] == "full"
        assert conclusion in answer["reason"]


def test_a_missing_run_keeps_the_tag_full(tmp_path: Path):
    answer = decide(tmp_path, runs_payload(conclusion=None))
    assert answer["mode"] == "full"
    assert "no completed push run" in answer["reason"]


def test_a_run_missing_one_gate_keeps_the_tag_full(tmp_path: Path):
    answer = decide(tmp_path, runs_payload(), jobs_payload(skip="smoke"))
    assert answer["mode"] == "full"
    assert "smoke" in answer["reason"]


def test_an_unsuccessful_gate_job_keeps_the_tag_full(tmp_path: Path):
    answer = decide(tmp_path, runs_payload(), jobs_payload(failing="integration-shard (3)"))
    assert answer["mode"] == "full"
    assert "integration-shard (3)" in answer["reason"]


def test_a_matrix_gate_reported_by_entry_must_have_passed_in_every_entry(tmp_path: Path):
    answer = decide(tmp_path, runs_payload(), jobs_payload(failing="image (git)"))
    assert answer["mode"] == "full"
    assert "image (git)" in answer["reason"]


def test_a_matrix_gate_with_no_entries_keeps_the_tag_full(tmp_path: Path):
    answer = decide(tmp_path, runs_payload(), jobs_payload(skip="image"))
    assert answer["mode"] == "full"
    assert "image" in answer["reason"]


def test_a_run_that_was_not_on_main_does_not_count(tmp_path: Path):
    """An earlier tag build on the same commit is not main having passed it."""
    answer = decide(tmp_path, runs_payload(branch="v1.2.2"), jobs_payload())
    assert answer["mode"] == "full"
    assert "no completed push run" in answer["reason"]


def test_the_decision_lands_in_the_step_outputs_and_the_summary(tmp_path: Path):
    decide(tmp_path, runs_payload(), jobs_payload())
    outputs = (tmp_path / "outputs").read_text(encoding="utf-8")
    summary = (tmp_path / "summary").read_text(encoding="utf-8")
    assert "mode=minimal" in outputs
    assert "reason=" in outputs
    assert outputs == summary


def test_the_shards_partition_the_file_list_exactly():
    def shard_list(shards: int, shard: int) -> list[str]:
        env = os.environ | {"SHARDS": str(shards), "SHARD": str(shard)}
        result = subprocess.run(
            ["bash", str(SHARD_SCRIPT)],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        return [line for line in result.stdout.splitlines() if line]

    whole = shard_list(1, 1)
    parts = [shard_list(4, number) for number in (1, 2, 3, 4)]
    flat = [name for part in parts for name in part]
    assert len(flat) == len(set(flat)) == len(whole), "shards must not miss or repeat a file"
    assert {name for part in parts for name in part} == set(whole)
    # Every file kept its order inside its own shard, which is what makes the
    # numbering stable between jobs.
    for part in parts:
        assert part == [name for name in whole if name in set(part)]


def run_coverage(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(COVERAGE_SCRIPT), str(directory)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )


def shard_files(shards: int = 4) -> dict[int, list[str]]:
    parts: dict[int, list[str]] = {}
    for number in range(1, shards + 1):
        env = os.environ | {"SHARDS": str(shards), "SHARD": str(number)}
        result = subprocess.run(
            ["bash", str(SHARD_SCRIPT)],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        parts[number] = [line for line in result.stdout.splitlines() if line]
    return parts


@functools.cache
def count_collected(files: tuple[str, ...]) -> int:
    """How many tests those files hold, read from pytest itself."""
    result = subprocess.run(
        ["uv", "run", "pytest", "-q", "--collect-only", *files],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    last = [line for line in result.stdout.splitlines() if "collected" in line][-1]
    return int(last.split()[0])


def write_shards(
    directory: Path,
    parts: dict[int, list[str]],
    *,
    skipped: int = 0,
    listed: dict[int, list[str]] | None = None,
) -> None:
    """Write what the shard jobs upload: the file list and pytest's own summary."""
    for number, files in parts.items():
        count = count_collected(tuple(files))
        shown = (listed or parts)[number]
        (directory / f"shard-{number}.list").write_text("\n".join(shown) + "\n", encoding="utf-8")
        summary = f"{count - skipped} passed, {skipped} skipped" if skipped else f"{count} passed"
        (directory / f"shard-{number}.txt").write_text(
            f"...\n{summary} in 12.34s\n", encoding="utf-8"
        )


def test_shards_that_together_ran_the_suite_once_pass_the_coverage_check(tmp_path: Path):
    write_shards(tmp_path, shard_files(), skipped=1)
    result = run_coverage(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "each in exactly one shard" in result.stdout


def test_a_shard_that_dropped_a_file_fails_the_coverage_check(tmp_path: Path):
    parts = shard_files()
    dropped = parts[2][0]
    write_shards(tmp_path, parts, listed={**parts, 2: parts[2][1:]})
    result = run_coverage(tmp_path)
    assert result.returncode != 0
    assert dropped in result.stderr


def test_a_file_run_by_two_shards_fails_the_coverage_check(tmp_path: Path):
    parts = shard_files()
    doubled = parts[1][0]
    write_shards(tmp_path, parts, listed={**parts, 2: [doubled, *parts[2]]})
    result = run_coverage(tmp_path)
    assert result.returncode != 0
    assert "2 shards" in result.stderr


def test_a_shard_whose_output_disagrees_with_its_list_fails_the_coverage_check(tmp_path: Path):
    parts = shard_files()
    write_shards(tmp_path, parts)
    (tmp_path / "shard-3.txt").write_text("1 passed in 0.10s\n", encoding="utf-8")
    result = run_coverage(tmp_path)
    assert result.returncode != 0
    assert "shard-3.list" in result.stderr


def test_a_shard_that_kept_no_output_fails_the_coverage_check(tmp_path: Path):
    parts = shard_files()
    write_shards(tmp_path, parts)
    (tmp_path / "shard-4.txt").unlink()
    result = run_coverage(tmp_path)
    assert result.returncode != 0
    assert "shard-4.list" in result.stderr


def test_no_shard_files_at_all_fails_the_coverage_check(tmp_path: Path):
    result = run_coverage(tmp_path)
    assert result.returncode != 0
