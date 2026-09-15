#!/usr/bin/env bash
# The simulated client proves the Obsidian Sync helper lifecycle without an
# account. This brings the stack up itself, layers the simulated client on top
# of whatever compose files it was given, and puts the helper back in its
# normal refusing mode with no connection left behind.
#
# Usage:
#   bash ci/sync-smoke.sh
#   COMPOSE_FILES="-f docker-compose.yml -f docker-compose.ci.yml" bash ci/sync-smoke.sh
set -euo pipefail

COMPOSE_FILES="${COMPOSE_FILES:--f docker-compose.yml}"
SIMULATED_FILES="$COMPOSE_FILES -f docker-compose.sync-smoke.yml"
# shellcheck disable=SC2086
compose() { docker compose $SIMULATED_FILES "$@"; }
# shellcheck disable=SC2086
plain_compose() { docker compose $COMPOSE_FILES "$@"; }

step() { printf '\n== %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

control() {
    compose exec -T obsidian-sync node -e '
const fs = require("fs");
const [method, route] = process.argv.slice(1);
const token = fs.readFileSync("/run/coppermind/internal/internal-token", "utf8").trim();
fetch(`http://127.0.0.1:8092${route}`, {
  method,
  headers: {authorization: `Bearer ${token}`, "content-type": "application/json"},
  body: method === "POST" ? "{}" : undefined,
}).then(async response => {
  const body = await response.text();
  process.stdout.write(body);
  if (!response.ok) process.exit(1);
});' "$1" "$2"
}

field() {
    python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

wait_for() {
    local wanted="$1" previous_pid="${2:-}" state pid
    for _ in $(seq 1 100); do
        state="$(control GET /status)"
        if [ "$(printf '%s' "$state" | field state)" = "$wanted" ]; then
            pid="$(printf '%s' "$state" | field sync_pid)"
            if [ -z "$previous_pid" ] || [ "$pid" != "$previous_pid" ]; then
                printf '%s' "$state"
                return 0
            fi
        fi
        sleep 0.1
    done
    fail "sync helper never reached $wanted"
}

step "bring the stack up with the simulated client"
compose up -d --wait --remove-orphans

initial="$(control GET /status)"
[ "$(printf '%s' "$initial" | field simulated)" = "True" ] \
    || fail "helper is not running the simulated client"
[ "$(printf '%s' "$initial" | field connected)" = "False" ] || fail "fresh helper claims connected"
[ "$(printf '%s' "$initial" | field syncing)" = "False" ] || fail "fresh helper claims syncing"

connected="$(control POST /connect)"
[ "$(printf '%s' "$connected" | field state)" = "syncing" ] || fail "connect did not start sync"

paused="$(control POST /pause)"
[ "$(printf '%s' "$paused" | field state)" = "paused" ] || fail "pause did not stop sync"
[ "$(printf '%s' "$paused" | field connected)" = "False" ] || fail "paused helper claims connected"

resumed="$(control POST /resume)"
[ "$(printf '%s' "$resumed" | field state)" = "syncing" ] || fail "resume did not start sync"
old_pid="$(printf '%s' "$resumed" | field sync_pid)"

compose exec -T obsidian-sync node -e '
const fs = require("fs");
const status = JSON.parse(fs.readFileSync("/data/state/sync/status.json", "utf8"));
process.kill(status.sync_pid, "SIGKILL");'
restarted="$(wait_for syncing "$old_pid")"
new_pid="$(printf '%s' "$restarted" | field sync_pid)"
[ "$new_pid" != "$old_pid" ] || fail "supervisor did not replace the killed process"

persisted="$(compose exec -T obsidian-sync cat /data/state/sync/status.json)"
[ "$(printf '%s' "$persisted" | field sync_pid)" = "$new_pid" ] \
    || fail "status file does not report the restarted process"
[ "$(printf '%s' "$persisted" | field syncing)" = "True" ] \
    || fail "status file does not report the active simulated client"
[ "$(printf '%s' "$persisted" | field sync_mode)" = "simulated" ] \
    || fail "status file does not report the client as simulated"

step "leave nothing simulated behind"
compose exec -T obsidian-sync rm -f /data/state/sync/connection.json
plain_compose up -d --wait --force-recreate --no-deps obsidian-sync
restored="$(plain_compose exec -T obsidian-sync cat /data/state/sync/status.json)"
[ "$(printf '%s' "$restored" | field simulated)" = "False" ] \
    || fail "the helper is still running the simulated client"
[ "$(printf '%s' "$restored" | field state)" = "not_connected" ] \
    || fail "the simulated connection outlived the smoke run"

printf '\nsimulated sync lifecycle passed: connect, pause, resume, killed process restarted\n'
