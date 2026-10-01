#!/usr/bin/env bash
# The simulated client proves the Obsidian Sync helper lifecycle without an
# account. This brings the stack up itself, layers the simulated client on top
# of whatever compose files it was given, and puts the helper back in its
# normal disconnected mode with no connection left behind.
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

# The packaged command the README tells operators to use, so this proves that
# too rather than a second client that only exists here.
control() {
    compose exec -T obsidian-sync node /app/control.mjs "$@"
}

field() {
    python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

wait_for() {
    local wanted="$1" previous_pid="${2:-}" state pid
    for _ in $(seq 1 100); do
        state="$(control status 2>/dev/null)" || { sleep 0.1; continue; }
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

# Runs however the script exits, so a failed assertion cannot leave a simulated
# connection on the volume for the next run to trip over.
restore_helper() {
    local code=$?
    step "leave nothing simulated behind"
    control disconnect || true
    rm -f "${admin_jar:-}"
    plain_compose up -d --wait --force-recreate --no-deps obsidian-sync || true
    [ "$code" -eq 0 ] || return
    local restored
    restored="$(plain_compose exec -T obsidian-sync cat /data/state/sync/status.json)"
    [ "$(printf '%s' "$restored" | field simulated)" = "False" ] \
        || fail "the helper is still running the simulated client"
    [ "$(printf '%s' "$restored" | field state)" = "not_connected" ] \
        || fail "the simulated connection outlived the smoke run"
    printf '\nsimulated sync lifecycle passed: connect, pause, resume, killed process restarted\n'
}

step "bring the stack up with the simulated client"
compose up -d --wait --remove-orphans
trap restore_helper EXIT
# The account token and vault encryption key must land on their own volume,
# not in the container writable layer and not in the data backup, so this
# proves the credential home is a mount point and a different one from /data.
# shellcheck disable=SC2016
compose exec -T obsidian-sync node -e '
const fs = require("fs");
const CREDENTIALS = "/var/lib/obsidian-sync";
const refuse = (why) => { console.error(why); process.exit(1); };
if (process.env.HOME !== CREDENTIALS) refuse(`HOME is ${process.env.HOME}, not ${CREDENTIALS}`);
try { fs.accessSync(CREDENTIALS, fs.constants.W_OK); } catch { refuse(`${CREDENTIALS} is not writable`); }
const mounts = fs
  .readFileSync("/proc/self/mountinfo", "utf8")
  .split("\n")
  .filter(Boolean)
  .map((line) => line.split(" "));
const mountOf = (target) => mounts.filter((fields) => fields[4] === target).pop();
const credentials = mountOf(CREDENTIALS);
const data = mountOf("/data");
if (!credentials) refuse(`${CREDENTIALS} is not a mount point: credentials would live in the container writable layer`);
if (!data) refuse("/data is not a mount point");
const volume = (fields) => `${fields[2]} ${fields[3]}`;
if (volume(credentials) === volume(data)) refuse(`${CREDENTIALS} is the same volume as /data, so credentials would sit in the data backup`);
' || fail "the credential volume is not isolated from /data"

initial="$(control status)"
[ "$(printf '%s' "$initial" | field simulated)" = "True" ] \
    || fail "helper is not running the simulated client"
[ "$(printf '%s' "$initial" | field syncing)" = "False" ] || fail "fresh helper claims syncing"

step "connect from the signed-in Admin page"
ADMIN="${ADMIN:-http://127.0.0.1:8082}"
admin_jar="$(mktemp)"
# Standalone smoke can claim a fresh installation; the full storyline has
# already claimed it with this same synthetic test password.
# Every Admin POST carries the session-bound CSRF token from the page that
# renders the form, the same way ci/smoke.sh does.
admin_page() { curl -sS -b "$admin_jar" -c "$admin_jar" "$@"; }
form_value() {
    python3 -c 'import sys
from html.parser import HTMLParser
class Inputs(HTMLParser):
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") == sys.argv[1]:
            print(attrs.get("value", ""))
Inputs().feed(sys.stdin.read())' "$1"
}
claim_code="$(compose exec -T admin sh -c 'cat /data/state/internal/claim-code 2>/dev/null || true')"
if [ -n "$claim_code" ]; then
    admin_csrf="$(admin_page "$ADMIN/admin/claim" | form_value csrf)"
    [ -n "$admin_csrf" ] || fail "Claim form has no CSRF token"
    curl -fsS -b "$admin_jar" -c "$admin_jar" -o /dev/null \
        --data-urlencode "csrf=$admin_csrf" --data-urlencode "code=$claim_code" \
        --data-urlencode 'password=smoke admin password' "$ADMIN/v1/admin/claim"
fi
admin_csrf="$(admin_page "$ADMIN/admin" | form_value csrf)"
[ -n "$admin_csrf" ] || fail "Sign-in form has no CSRF token"
curl -fsS -b "$admin_jar" -c "$admin_jar" -o /dev/null \
    --data-urlencode "csrf=$admin_csrf" --data-urlencode 'password=smoke admin password' \
    "$ADMIN/v1/admin/login"
sync_page="$(admin_page -f "$ADMIN/admin/sync")" || fail "Admin did not render Connect"
printf '%s' "$sync_page" | grep -Fq 'Encryption password' || fail "Admin did not render Connect"
admin_csrf="$(printf '%s' "$sync_page" | form_value csrf)"
[ -n "$admin_csrf" ] || fail "Sync page has no CSRF token"
admin_action() {
    local action="$1" form="${2:-submit=yes}" result
    result="$(printf 'csrf=%s&%s' "$admin_csrf" "$form" | curl -fsS -b "$admin_jar" -o /dev/null \
        -w '%{redirect_url}' --data-binary @- "$ADMIN/admin/sync/$action")"
    [ "$result" = "$ADMIN/admin/sync?result=saved" ] || fail "Admin $action failed"
}
admin_action connect 'email=smoke%40example.invalid&password=simulated-account&encryption_password=simulated-encryption&encryption_confirm=simulated-encryption&mfa_code=&vault_name=Simulated+remote+vault&device_name=coppermind-server&plan=standard&existing_vault=false'
connected="$(control status)"
[ "$(printf '%s' "$connected" | field state)" = "syncing" ] || fail "connect did not start sync"
[ "$(printf '%s' "$connected" | field vault_name)" = "Simulated remote vault" ] \
    || fail "connect did not record the vault name it was given"

admin_action pause
paused="$(control status)"
[ "$(printf '%s' "$paused" | field state)" = "paused" ] || fail "pause did not stop sync"

admin_action resume
resumed="$(control status)"
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

step "restart the helper with its persisted connection"
compose restart obsidian-sync
wait_for syncing >/dev/null
admin_action disconnect
[ "$(control status | field state)" = "not_connected" ] || fail "disconnect did not clear state"
