# Proof that the parallel shards are a partition of the integration suite: the
# tests across all shards are exactly the tests a single run collects, each one
# appearing once.
#
# Each shard job keeps two files in `$RUNNER_TEMP`, which the runner shares between
# jobs of one run: `shard-<n>.list`, the file list it ran, and `shard-<n>.txt`, the
# pytest output it printed. A job's own exit code cannot show coverage across jobs,
# which is what this checks. Both are read by their own names, so a missing one is a
# failure rather than a silent gap.
: "${RUNNER_TEMP:?this check runs as a CI job with a runner temp directory}"

python3 - "$RUNNER_TEMP" <<'PY'
import os
import re
import subprocess
import sys

temp = sys.argv[1]
names = sorted(
    name for name in os.listdir(temp) if name.startswith("shard-") and name.endswith(".list")
)
if not names:
    print(f"no shard lists in {temp}, nothing to aggregate")
    raise SystemExit(0)


def collected(*paths: str) -> int:
    """How many tests one or more files hold, as pytest counts them."""
    if not paths:
        return 0
    result = subprocess.run(
        ["uv", "run", "pytest", "-q", "--collect-only", *paths],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return next(
        (int(line.split()[0]) for line in result.splitlines() if "tests collected" in line), 0
    )


def read(name: str) -> str:
    path = os.path.join(temp, name)
    if not os.path.exists(path):
        return ""
    return open(path, encoding="utf-8").read()


def files_of(name: str) -> list[str]:
    return [line.strip() for line in read(name).splitlines() if line.strip().startswith("tests/")]


problems = []
owned: dict[str, int] = {}
counts: dict[str, int] = {}
for name in names:
    paths = files_of(name)
    counts[name] = collected(*paths)
    for path in paths:
        owned[path] = owned.get(path, 0) + 1

problems += [f"{path} is in {count} shards" for path, count in sorted(owned.items()) if count != 1]

whole = subprocess.run(
    ["bash", "ci/shard-list.sh"],
    capture_output=True,
    text=True,
    check=True,
    # The unsplit list, whatever number of shards the job set.
    env={**os.environ, "SHARDS": "1", "SHARD": "1"},
).stdout.split()
if sorted(owned) != sorted(whole):
    missing = sorted(set(whole) - set(owned))
    extra = sorted(set(owned) - set(whole))
    if missing:
        problems.append(f"no shard ran {missing}")
    if extra:
        problems.append(f"a shard ran files the whole list does not have: {extra}")

total = sum(counts.values())
single = collected("tests/integration")
if total != single:
    problems.append(f"shards together ran {total} tests, a single run collects {single}")

# A shard whose own output disagrees with its file list ran something else. The
# printed total is the sum of its two counts, since a shard may skip a test.
for name in names:
    body = read(name[: -len(".list")] + ".txt")
    if not body:
        problems.append(f"{name}: no pytest output kept for this shard")
        continue
    counts_found = dict(
        ({"passed": 1, "failed": 1, "skipped": 1}.get(match.group(2)), int(match.group(1)))
        for match in re.finditer(r"(\d+) (passed|failed|skipped)", body)
    )
    printed = sum(counts_found.values())
    if printed and printed != counts[name]:
        problems.append(f"{name}: printed {printed} tests, its files hold {counts[name]}")

if problems:
    print("\n".join(f"FAIL: {problem}" for problem in problems), file=sys.stderr)
    raise SystemExit(1)
print(f"shard lists: {len(names)}")
print(f"ok: {single} tests over {len(whole)} files, each in exactly one shard")
PY
