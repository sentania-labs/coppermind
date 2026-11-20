#!/usr/bin/env bash
set -euo pipefail

# Decide whether a tag build may skip the long test suites.
#
# The answers come from the GitHub API: the completed run of this workflow on the
# tagged commit, and the conclusion of each of its jobs. Prints `mode` and `reason`
# for the job log and the job summary, and, with a path as the first argument, as
# step outputs.
#
# A minimal release is only sound when every gate a full run would have gone
# through has already passed for this exact commit, so the probe requires each of
# those job names to report success. A lookup that yielded nothing, or a run that
# is missing one of them, is not evidence of a passing run: the tag then runs the
# full suite.
#
# Usage, in a workflow step where the GitHub token is set:
#   bash ci/release-mode.sh                      # reads the answers from the log
#   bash ci/release-mode.sh "$GITHUB_OUTPUT"     # and as step outputs
#
# A test may set RELEASE_JSON and RELEASE_JOBS to skip the network and fix the
# answers itself; the workflow leaves both unset. Their shape is documented in
# `ci/release-mode-data.sh`.
: "${GITHUB_SHA:?build commit is required}"

# The job names a full run must have gone green on, as one comma separated list.
FULL_JOBS="${FULL_JOBS:-checks,scan,release-tag,image,smoke,integration-shard}"
output_file="${1:-${GITHUB_STEP_SUMMARY:-}}"

answer() { # answer <mode> <reason>
  echo "mode=$1"
  echo "reason=$2"
  [ -n "${GITHUB_STEP_SUMMARY:-}" ] && printf 'mode=%s\nreason=%s\n' "$1" "$2" >>"$GITHUB_STEP_SUMMARY"
  [ -n "$output_file" ] && printf 'mode=%s\nreason=%s\n' "$1" "$2" >>"$output_file"
  [ "$1" = "full" ] && return 0
  return 0
}

read_answers() { # prints `N|conclusion|name=conclusion,...` for this commit
  python3 - "$GITHUB_SHA" "$FULL_JOBS" <<'PY'
import json
import os
import sys

sha, wanted = sys.argv[1], sys.argv[2]
document = json.loads(os.environ.get("RELEASE_JSON") or "{}")
jobs_document = json.loads(os.environ.get("RELEASE_JOBS") or "{}")

run = next(
    (
        candidate
        for candidate in document.get("workflow_runs") or []
        if candidate.get("head_sha") == sha
        and candidate.get("event") == "push"
        and candidate.get("status") == "completed"
    ),
    None,
)
if run is None:
    print("")
    raise SystemExit(0)

# A job list read separately wins; otherwise the one nested under the run.
jobs = (jobs_document.get("jobs") or run.get("jobs") or []) if jobs_document else run.get("jobs") or []
pairs = ",".join(f"{job.get('name')}={job.get('conclusion')}" for job in jobs)
print(f"{run.get('run_number')}|{run.get('conclusion')}|{pairs}")
PY
}

if [ -z "${RELEASE_JSON:-}" ]; then
  RELEASE_JSON="$(curl -sS --get "${GITHUB_API_URL:-https://api.github.com}/repos/${GITHUB_REPOSITORY:?GitHub repository is required}/actions/runs" \
    --header "Authorization: Bearer ${GITHUB_TOKEN:?a workflow token is required}" \
    --data-urlencode "per_page=20" \
    --data-urlencode "head_sha=$GITHUB_SHA" \
    --data-urlencode "event=push" \
    --data-urlencode "status=completed")" || RELEASE_JSON=""
  # The job list is a second document, so fetch it once the run is known.
  if [ -n "$RELEASE_JSON" ]; then
    jobs_url="$(printf '%s' "$RELEASE_JSON" | python3 -c 'import json,os,sys
d=json.loads(sys.stdin.read() or "{}")
run=next((c for c in d.get("workflow_runs") or [] if c.get("head_sha")==os.environ["GITHUB_SHA"] and c.get("event")=="push" and c.get("status")=="completed"),None)
print((run or {}).get("jobs_url") or "")')"
    if [ -n "$jobs_url" ]; then
      RELEASE_JOBS="$(curl -sS --header "Authorization: Bearer ${GITHUB_TOKEN}" "$jobs_url")" || RELEASE_JOBS=""
      export RELEASE_JOBS
    fi
  fi
  export RELEASE_JSON
fi

line="$(read_answers)"
if [ -z "$line" ]; then
  answer full "no completed push run of this workflow for ${GITHUB_SHA:0:7}"
  exit 0
fi

number="${line%%|*}"
rest="${line#*|}"
conclusion="${rest%%|*}"
pairs="${rest#*|}"

if [ "$conclusion" != "success" ]; then
  answer full "run #$number on ${GITHUB_SHA:0:7} concluded $conclusion"
  exit 0
fi

if [ -z "$pairs" ]; then
  answer full "run #$number reports no jobs, so its gates cannot be read"
  exit 0
fi

statuses=",$pairs,"
missing=()
for name in ${FULL_JOBS//,/ }; do
  case "$statuses" in
    *",$name="*)
      got="${statuses#*",$name="}"
      got="${got%%,*}"
      [ "$got" = "success" ] || {
        answer full "run #$number job $name concluded $got"
        exit 0
      }
      ;;
    *) missing+=("$name") ;;
  esac
done

if [ ${#missing[@]} -gt 0 ]; then
  answer full "run #$number does not report the job(s) ${missing[*]}"
  exit 0
fi

answer minimal "run #$number passed every full-suite gate on ${GITHUB_SHA:0:7}"
