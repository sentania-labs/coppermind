#!/usr/bin/env bash
# Failure storylines, run against real images. Today there is one, T-GIT-1:
# stop the Git helper, change notes, start it, and history catches up. Along
# the way it proves the three things the helper promises: the commits carry
# the actual note changes, earlier history survives every restart, and
# Obsidian's device state, deleted notes and generated projections never enter
# history. Read with `git` on the volume, never through the API, because every
# claim here is about the repository.
#
# The helper polls every `git.poll_interval_s` (300 seconds by default) and
# commits once a change has been quiet for `git.debounce_s` (60). A start scans
# at once, so this storyline takes about two minutes rather than twelve; the
# timing of the running loop is covered by the helper's unit tests.
#
# Usage:
#   bash ci/failure.sh
#   COMPOSE_FILES="-f docker-compose.yml -f docker-compose.ci.yml" bash ci/failure.sh
set -euo pipefail

COMPOSE_FILES="${COMPOSE_FILES:--f docker-compose.yml}"
API="${API:-http://127.0.0.1:8080}"
# shellcheck disable=SC2086
compose() { docker compose $COMPOSE_FILES "$@"; }

step()  { printf '\n=== %s\n' "$1"; }
fail()  { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
ok()    { printf 'ok: %s\n' "$1"; }

notes_git() { compose exec -T git git -C /data/notes -c core.quotePath=false "$@"; }
head_or_none() { notes_git rev-parse -q --verify HEAD 2>/dev/null || echo none; }
# Stands in for a person editing on a device: a write the store did not make.
device() { compose exec -T store sh -c "$1" sh "${@:2}"; }

# Where history stood once the helper was stopped. Taken after the stop on
# purpose: a baseline read while the helper still runs can be overtaken by a
# commit of its own, and the next wait would then return that commit instead of
# the catch-up one. The helper is stopped here, so its status file is the only
# thing that can answer, and the store container is what reads the volume.
baseline_after_stop() {
    compose exec -T store cat /data/state/git/status.json 2>/dev/null \
        | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("last_commit_sha") or "none")
except ValueError:
    print("none")'
}

wait_for_commit_after() {
    local before="$1" now=""
    for _ in $(seq 1 150); do
        now="$(head_or_none)"
        if [ "$now" != "none" ] && [ "$now" != "$before" ]; then
            printf '%s' "$now"
            return 0
        fi
        sleep 1
    done
    fail "no new commit within 150 seconds (HEAD is still $before)"
}

step "bring the stack up"
compose up -d --wait --remove-orphans
ok "compose reported every service healthy, the Git helper included"

step "the helper made the notes filesystem a repository on its own"
for _ in $(seq 1 30); do
    notes_git rev-parse --git-dir >/dev/null 2>&1 && break
    sleep 1
done
notes_git rev-parse --git-dir >/dev/null 2>&1 || fail "/data/notes is not a repository"
[ -z "$(notes_git remote)" ] || fail "the repository has a remote"
ok "/data/notes/.git exists and names no remote"

step "stop the helper, then change notes behind its back"
compose stop git
before="$(baseline_after_stop)"
created="$(mktemp)"
code="$(curl -sS -o "$created" -w '%{http_code}' -X POST "$API/v1/notes" \
    -H 'Content-Type: application/json' \
    -d '{"title":"Git Helper Catch Up","body":"- written while the helper was stopped\n","frontmatter":{"type":"reference","context":"internal"}}')"
[ "$code" = "201" ] || { cat "$created"; fail "create returned $code, expected 201"; }
note_path="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["path"])' "$created")"
device 'mkdir -p /data/notes/.obsidian /data/notes/.trash /data/notes/_Trash "/data/notes/_Sources/Plaud"
        echo "{\"lastOpenFiles\":[]}" > /data/notes/.obsidian/workspace.json
        echo "deleted on a device" > /data/notes/.trash/old.md
        echo "deleted through Coppermind" > /data/notes/_Trash/gone.md
        echo "generated projection" > "/data/notes/_Sources/Plaud/2026-09-08 Call.md"'
ok "created $note_path through the API and wrote excluded material on the volume"

step "start the helper and wait for the catch-up commit"
compose start git
first="$(wait_for_commit_after "$before")"
notes_git log -1 --format='%h %an <%ae> %s' "$first"
notes_git log -1 --format=%s "$first" | grep -q '^Coppermind snapshot' \
    || fail "the commit is not a Coppermind snapshot"
notes_git ls-tree -r --name-only "$first" | grep -Fqx -- "$note_path" \
    || fail "the note is not in the commit"
tree="$(notes_git ls-tree -r --name-only "$first")"
for excluded in .obsidian/ .trash/ _Trash/ _Sources/; do
    if printf '%s\n' "$tree" | grep -Fq -- "$excluded"; then
        fail "$excluded entered history"
    fi
done
ok "commit $first holds the note and none of .obsidian, .trash, _Trash or _Sources"

step "edit the note while the helper is stopped again"
root="$(notes_git rev-list --max-parents=0 HEAD)"
compose stop git
before_second="$(baseline_after_stop)"
device 'printf "%s\n" "- edited on a device while the helper was stopped" >> "/data/notes/$1"' "$note_path"
compose start git
second="$(wait_for_commit_after "$before_second")"
notes_git show --format='%h %s' "$second" -- "$note_path"

step "history survived both restarts and carries the edit"
[ "$(notes_git rev-parse "$second~1")" = "$before_second" ] \
    || fail "the new commit does not follow the history that was already there"
[ "$(notes_git rev-list --max-parents=0 HEAD)" = "$root" ] || fail "the root commit changed"
notes_git show "$second" -- "$note_path" | grep -Fqx -- "+- edited on a device while the helper was stopped" \
    || fail "the commit does not carry the edited line"
status_json="$(compose exec -T git cat /data/state/git/status.json)"
printf '%s\n' "$status_json"
python3 -c 'import json,sys; s=json.loads(sys.argv[1]); assert s["last_commit_sha"]==sys.argv[2] and s["last_error"] is None, s' \
    "$status_json" "$second" || fail "the status file does not report the latest commit cleanly"
notes_git log --max-count=5 --format='%h %s'
ok "the edit is recorded on top of unchanged history and the status file agrees"

printf '\nfailure storylines passed\n'
