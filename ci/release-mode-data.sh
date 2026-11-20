# The API answers the release-mode decision is made from, in two variables.
#
# `RELEASE_JSON` holds the runs document for one commit, `RELEASE_JOBS` the job list
# of the run that document names. The workflow fills both with `curl`; a test sets
# them directly so no network is needed. Each is the GitHub Actions shape:
#
#   RELEASE_JSON='{"workflow_runs": [{"run_number": 42, "head_sha": "...",
#     "event": "push", "status": "completed", "conclusion": "success",
#     "jobs_url": "..."}]}'
#   RELEASE_JOBS='{"jobs": [{"name": "checks", "conclusion": "success"}, ...]}'
#
# An empty `RELEASE_JSON` means the lookup found nothing, which is the same answer
# as a run that did not finish green: the tag runs the full suite.
