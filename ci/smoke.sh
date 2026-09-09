#!/usr/bin/env bash
# The compose storyline for slice 1 PR 1, run against real images.
#
# It proves the four things this PR claims: the stack comes up from a clean
# checkout with no manual setup, a note created through the API lands as a
# Markdown file with correct frontmatter in the notes filesystem, readiness
# fails honestly when PostgreSQL is stopped, and everything recovers when it
# returns. Read from the volume, never through the API, whenever the claim is
# about a file.
#
# Usage:
#   bash ci/smoke.sh
#   COMPOSE_FILES="-f docker-compose.yml -f docker-compose.ci.yml" bash ci/smoke.sh
set -euo pipefail

COMPOSE_FILES="${COMPOSE_FILES:--f docker-compose.yml}"
API="${API:-http://127.0.0.1:8080}"
# shellcheck disable=SC2086
compose() { docker compose $COMPOSE_FILES "$@"; }

step()  { printf '\n=== %s\n' "$1"; }
fail()  { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
ok()    { printf 'ok: %s\n' "$1"; }

status_of() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }

wait_for_status() {
    local want="$1" url="$2" tries="${3:-60}" got=""
    for _ in $(seq 1 "$tries"); do
        got="$(status_of "$url" || true)"
        [ "$got" = "$want" ] && return 0
        sleep 1
    done
    fail "$url answered $got, expected $want"
}

step "bring the stack up"
compose up -d --wait --remove-orphans
ok "compose reported every service healthy"

step "the API is up and ready"
wait_for_status 200 "$API/healthz"
wait_for_status 200 "$API/readyz"
curl -sS "$API/readyz"; echo
ok "/healthz and /readyz answer 200"

step "credentials are limited to the services that need them"
compose exec -T api test ! -r /run/coppermind/postgres/postgres-password \
    || fail "the API can read the PostgreSQL password"
compose exec -T store test -r /run/coppermind/postgres/postgres-password \
    || fail "the store cannot read the PostgreSQL password"
ok "the API cannot read the database password and the store can"

step "create a note through the API"
created="$(mktemp)"
code="$(curl -sS -o "$created" -w '%{http_code}' -X POST "$API/v1/notes" \
    -H 'Content-Type: application/json' \
    -d '{"title":"Ameren Architecture Sync","body":"## Key points\n- Target architecture agreed\n","frontmatter":{"date":"2026-09-08","type":"meeting","context":"customer","account":"Ameren","tags":["architecture","vcf"]}}')"
[ "$code" = "201" ] || { cat "$created"; fail "create returned $code, expected 201"; }
cat "$created"; echo
note_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$created")"
note_path="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["path"])' "$created")"
etag="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["content_hash"])' "$created")"
ok "created $note_id at $note_path"

step "read the file on the volume, not through the API"
on_disk="$(compose exec -T store cat "/data/notes/$note_path")"
printf '%s\n' "$on_disk"
for key in "id: $note_id" "schema_version: 1" "date: 2026-09-08" "type: meeting" \
           "context: customer" "account: Ameren" "reviewed: false" "sources: []"; do
    printf '%s\n' "$on_disk" | grep -Fqx -- "$key" || fail "frontmatter is missing '$key'"
done
printf '%s\n' "$on_disk" | grep -Fqx -- "# Ameren Architecture Sync" || fail "the H1 is missing"
ok "the file carries its identifier and the shipped frontmatter keys"

step "read the note back through the API"
fetched="$(mktemp)"
code="$(curl -sS -o "$fetched" -w '%{http_code}' "$API/v1/notes/$note_id")"
[ "$code" = "200" ] || { cat "$fetched"; fail "get returned $code, expected 200"; }
fetched_hash="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["content_hash"])' "$fetched")"
[ "$fetched_hash" = "$etag" ] || fail "content hash changed between create and read"
ok "the note reads back with the same ETag"

review_count() { compose exec -T store sh -c 'ls -1 /data/notes/Review | wc -l' | tr -d "[:space:]"; }
before_outage="$(review_count)"

step "stop PostgreSQL and check readiness tells the truth"
compose stop postgres
wait_for_status 503 "$API/readyz" 30
curl -sS "$API/readyz"; echo
down_write="$(curl -sS -X POST "$API/v1/notes" -H 'Content-Type: application/json' \
    -d '{"title":"Written while the database is down"}')"
printf '%s\n' "$down_write"
printf '%s\n' "$down_write" | grep -q 'metadata_unavailable' \
    || fail "a write during the outage did not report metadata_unavailable"
[ "$(status_of "$API/v1/notes/$note_id")" = "503" ] \
    || fail "a read during the outage did not return 503"
ok "readiness is 503 and mutations return a clean metadata_unavailable"

step "the notes filesystem is untouched by the outage"
still_there="$(compose exec -T store cat "/data/notes/$note_path")"
[ "$still_there" = "$on_disk" ] || fail "the note file changed during the outage"
[ "$(review_count)" = "$before_outage" ] \
    || fail "the refused write left a file behind in the review folder"
ok "the note is unchanged and the refused write left no orphan file"

step "start PostgreSQL and check everything recovers"
compose start postgres
wait_for_status 200 "$API/readyz" 60
curl -sS "$API/readyz"; echo
recovered="$(mktemp)"
code="$(curl -sS -o "$recovered" -w '%{http_code}' "$API/v1/notes/$note_id")"
[ "$code" = "200" ] || { cat "$recovered"; fail "the note did not come back after recovery"; }
recovered_hash="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["content_hash"])' "$recovered")"
[ "$recovered_hash" = "$etag" ] || fail "the note changed across the outage"
ok "readiness is 200 again and the note is unchanged"

printf '\nsmoke passed\n'
