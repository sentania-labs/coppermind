#!/usr/bin/env bash
# The compose storyline, run against real images.
#
# It proves what the shipped slices claim: the stack comes up from a clean
# checkout with no manual setup, a note created through the API lands as a
# Markdown file with correct frontmatter in the notes filesystem, a write
# conditional on a stale ETag cannot overwrite an edit made on the volume
# while one carrying the current ETag lands, readiness fails honestly when
# PostgreSQL is stopped, and everything recovers when it returns. Read from
# the volume, never through the API, whenever the claim is about a file.
#
# Usage:
#   bash ci/smoke.sh
#   COMPOSE_FILES="-f docker-compose.yml -f docker-compose.ci.yml" bash ci/smoke.sh
set -euo pipefail

COMPOSE_FILES="${COMPOSE_FILES:--f docker-compose.yml}"
API="${API:-http://127.0.0.1:8080}"
# The API's bounded key cache life, as README.md and STATUS.md document it.
KEY_CACHE_SECONDS=300
# shellcheck disable=SC2086
compose() { docker compose $COMPOSE_FILES "$@"; }

step()  { printf '\n=== %s\n' "$1"; }
fail()  { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
ok()    { printf 'ok: %s\n' "$1"; }

status_of() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
field()     { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"; }

# The file as the store sees it, hashed inside the container so the claim is
# about the bytes on the volume and not about what survived a shell pipeline.
hash_on_volume() {
    compose exec -T store python3 -c \
        'import hashlib,sys; print("sha256:" + hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
        "/data/notes/$1" | tr -d '[:space:]'
}

# Sending the document a read returned back with its body extended, which is
# the read, edit, write round trip a client does.
edited_document() {
    python3 -c 'import json,sys
document = json.load(open(sys.argv[1]))
document["body"] += "\n- Corrected on review\n"
json.dump(document, open(sys.argv[2], "w"))' "$1" "$2"
}

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
wait_for_status 200 "$API/openapi.json"
ok "/healthz, /readyz and OpenAPI answer 200 without a key"

step "the bootstrapped default key is usable without a setup step"
default_key="$(compose run --rm --no-deps --entrypoint cat bootstrap \
    /run/coppermind/api/default-api-key | tr -d '[:space:]')"
[ -n "$default_key" ] || fail "bootstrap did not surface a default API key"
wrong_scope_key="$(compose exec -T store python3 -m coppermind_store.keys create \
    --name smoke-read-only --scope notes:read | tr -d '[:space:]')"
[ -n "$wrong_scope_key" ] || fail "the key command did not return a key"
AUTH=(-H "Authorization: Bearer $default_key")

code="$(status_of -X POST "$API/v1/notes" -H 'Content-Type: application/json' \
    -d '{"title":"No credential"}')"
[ "$code" = "401" ] || fail "a create with no key returned $code, expected 401"
# A key minted after the API last loaded its key cache is not seen until that
# cache expires, so the refusal is polled for within the documented lifetime.
deadline=$(( SECONDS + KEY_CACHE_SECONDS + 10 ))
while :; do
    code="$(status_of -X POST "$API/v1/notes" -H "Authorization: Bearer $wrong_scope_key" \
        -H 'Content-Type: application/json' -d '{"title":"Wrong scope"}')"
    [ "$code" = "401" ] || break
    [ "$SECONDS" -lt "$deadline" ] || break
    sleep 5
done
[ "$code" = "403" ] || fail "a create with a read-only key returned $code, expected 403"
ok "no key is refused with 401 and a read-only key is refused with 403"

step "credentials are limited to the services that need them"
compose exec -T api test ! -r /run/coppermind/postgres/postgres-password \
    || fail "the API can read the PostgreSQL password"
compose exec -T store test -r /run/coppermind/postgres/postgres-password \
    || fail "the store cannot read the PostgreSQL password"
ok "the API cannot read the database password and the store can"

step "ingest the sample source and its linked Review note"
ingested="$(mktemp)"
code="$(curl -sS -o "$ingested" -w '%{http_code}' -X POST "$API/v1/ingest" \
    "${AUTH[@]}" -H 'Content-Type: application/json' \
    --data-binary @examples/ingest/plaud-sample.json)"
[ "$code" = "201" ] || { cat "$ingested"; fail "ingest returned $code, expected 201"; }
cat "$ingested"; echo
source_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source"]["id"])' "$ingested")"
ingest_note_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["note"]["id"])' "$ingested")"
ingest_note_path="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["note"]["path"])' "$ingested")"
compose exec -T store test -f "/data/sources/$source_id/manifest.json" \
    || fail "the source manifest is missing"
compose exec -T store test -f "/data/sources/$source_id/r0001/transcript.txt" \
    || fail "the transcript artifact is missing"
ingest_note="$(compose exec -T store cat "/data/notes/$ingest_note_path")"
printf '%s\n' "$ingest_note" | grep -Fqx -- "  - $source_id" \
    || fail "the Review note does not link the source id"
[ "$(compose exec -T store sh -c 'find /data/sources -mindepth 1 -maxdepth 1 -type d | wc -l' | tr -d '[:space:]')" = "1" ] \
    || fail "the ingest created more than one source bundle"
[ "$(compose exec -T store sh -c 'find /data/notes/Review -maxdepth 1 -type f -name "*.md" | wc -l' | tr -d '[:space:]')" = "1" ] \
    || fail "the ingest created more than one Review note"
ok "source $source_id and note $ingest_note_id exist and are linked"

step "repeat the external id and prove the original stays unchanged"
source_before="$(compose exec -T store sh -c "find '/data/sources/$source_id' -type f -exec sha256sum {} \\; | sort")"
note_before="$(hash_on_volume "$ingest_note_path")"
duplicate="$(mktemp)"
code="$(curl -sS -o "$duplicate" -w '%{http_code}' -X POST "$API/v1/ingest" \
    "${AUTH[@]}" -H 'Content-Type: application/json' \
    --data-binary @examples/ingest/plaud-sample.json)"
[ "$code" = "409" ] || { cat "$duplicate"; fail "repeat ingest returned $code, expected 409"; }
[ "$(field "$duplicate" error)" = "source_exists" ] \
    || fail "the repeated external id was not reported as source_exists"
[ "$(compose exec -T store sh -c 'find /data/sources -mindepth 1 -maxdepth 1 -type d | wc -l' | tr -d '[:space:]')" = "1" ] \
    || fail "the repeated external id created another source bundle"
[ "$(compose exec -T store sh -c 'find /data/notes/Review -maxdepth 1 -type f -name "*.md" | wc -l' | tr -d '[:space:]')" = "1" ] \
    || fail "the repeated external id created another Review note"
[ "$(compose exec -T store sh -c "find '/data/sources/$source_id' -type f -exec sha256sum {} \\; | sort")" = "$source_before" ] \
    || fail "the repeated external id changed the source bundle"
[ "$(hash_on_volume "$ingest_note_path")" = "$note_before" ] \
    || fail "the repeated external id changed the Review note"
ok "the repeated external id returned 409, with one unchanged source and note"

step "create a note through the API"
created="$(mktemp)"
code="$(curl -sS -o "$created" -w '%{http_code}' -X POST "$API/v1/notes" \
    "${AUTH[@]}" \
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
code="$(curl -sS -o "$fetched" -w '%{http_code}' "${AUTH[@]}" "$API/v1/notes/$note_id")"
[ "$code" = "200" ] || { cat "$fetched"; fail "get returned $code, expected 200"; }
fetched_hash="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["content_hash"])' "$fetched")"
[ "$fetched_hash" = "$etag" ] || fail "content hash changed between create and read"
ok "the note reads back with the same ETag"

step "edit the file on the volume behind the store's back, as a device would"
# Through the store container's shell only because it is the one container
# that mounts the volume; nothing of Coppermind's writes here. The reconciler
# work adds a tools container for this.
compose exec -T store python3 -c \
    'import sys; p = sys.argv[1]; t = open(p).read(); open(p, "w").write(t.replace("reviewed: false", "reviewed: true", 1))' \
    "/data/notes/$note_path"
device_edit="$(compose exec -T store cat "/data/notes/$note_path")"
[ "$device_edit" != "$on_disk" ] || fail "the edit on the volume did not change the file"
device_hash="$(hash_on_volume "$note_path")"
[ "$device_hash" != "$etag" ] || fail "the edit on the volume did not change the hash"
ok "the file now carries reviewed: true and hashes to $device_hash"

step "a write with no If-Match is refused with 428"
document="$(mktemp)"
edited_document "$fetched" "$document"
code="$(status_of -X PUT "$API/v1/notes/$note_id" "${AUTH[@]}" \
    -H 'Content-Type: application/json' \
    --data-binary @"$document")"
[ "$code" = "428" ] || fail "an unconditional write returned $code, expected 428"
ok "an unconditional write is refused"

step "a write with the ETag from before the edit is refused with 409 and the current ETag"
conflict="$(mktemp)"
code="$(curl -sS -o "$conflict" -w '%{http_code}' -X PUT "$API/v1/notes/$note_id" \
    "${AUTH[@]}" -H 'Content-Type: application/json' -H "If-Match: \"$etag\"" \
    --data-binary @"$document")"
[ "$code" = "409" ] || { cat "$conflict"; fail "a stale write returned $code, expected 409"; }
cat "$conflict"; echo
[ "$(field "$conflict" error)" = "version_conflict" ] || fail "the stale write was not a version_conflict"
[ "$(field "$conflict" current_version)" = "$device_hash" ] \
    || fail "current_version is not the hash of the file on the volume"
[ "$(compose exec -T store cat "/data/notes/$note_path")" = "$device_edit" ] \
    || fail "the refused write changed the file"
ok "the stale write was refused and the edit made on the volume survived"

step "read again and write with the current ETag"
code="$(curl -sS -o "$fetched" -w '%{http_code}' "${AUTH[@]}" "$API/v1/notes/$note_id")"
[ "$code" = "200" ] || { cat "$fetched"; fail "get returned $code, expected 200"; }
current_etag="$(field "$fetched" content_hash)"
[ "$current_etag" = "$device_hash" ] || fail "the API does not see the edit made on the volume"
edited_document "$fetched" "$document"
replaced="$(mktemp)"
code="$(curl -sS -o "$replaced" -w '%{http_code}' -X PUT "$API/v1/notes/$note_id" \
    "${AUTH[@]}" -H 'Content-Type: application/json' -H "If-Match: \"$current_etag\"" \
    --data-binary @"$document")"
[ "$code" = "200" ] || { cat "$replaced"; fail "a current write returned $code, expected 200"; }
cat "$replaced"; echo
etag="$(field "$replaced" content_hash)"
[ "$etag" != "$current_etag" ] || fail "the ETag did not change after a successful write"
[ "$(field "$replaced" id)" = "$note_id" ] || fail "the identifier changed"
[ "$(field "$replaced" path)" = "$note_path" ] || fail "the path changed"
on_disk="$(compose exec -T store cat "/data/notes/$note_path")"
printf '%s\n' "$on_disk"
for line in "id: $note_id" "reviewed: true" "date: 2026-09-08" "- Corrected on review"; do
    printf '%s\n' "$on_disk" | grep -Fqx -- "$line" || fail "the replaced file is missing '$line'"
done
[ "$(hash_on_volume "$note_path")" = "$etag" ] || fail "the new ETag is not the hash of the file on the volume"
ok "the current write landed under the same identifier and path, with a fresh ETag"

review_count() { compose exec -T store sh -c 'ls -1 /data/notes/Review | wc -l' | tr -d "[:space:]"; }
before_outage="$(review_count)"

step "stop PostgreSQL and check readiness tells the truth"
compose stop postgres
wait_for_status 503 "$API/readyz" 30
curl -sS "$API/readyz"; echo
down_write="$(curl -sS -X POST "$API/v1/notes" "${AUTH[@]}" \
    -H 'Content-Type: application/json' \
    -d '{"title":"Written while the database is down"}')"
printf '%s\n' "$down_write"
printf '%s\n' "$down_write" | grep -q 'metadata_unavailable' \
    || fail "a write during the outage did not report metadata_unavailable"
down_replace="$(curl -sS -X PUT "$API/v1/notes/$note_id" "${AUTH[@]}" \
    -H 'Content-Type: application/json' -H "If-Match: \"$etag\"" \
    --data-binary @"$document")"
printf '%s\n' "$down_replace"
printf '%s\n' "$down_replace" | grep -q 'metadata_unavailable' \
    || fail "a replace during the outage did not report metadata_unavailable"
[ "$(status_of "${AUTH[@]}" "$API/v1/notes/$note_id")" = "503" ] \
    || fail "a read during the outage did not return 503"
ok "readiness is 503 and both mutations return a clean metadata_unavailable"

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
code="$(curl -sS -o "$recovered" -w '%{http_code}' "${AUTH[@]}" \
    "$API/v1/notes/$note_id")"
[ "$code" = "200" ] || { cat "$recovered"; fail "the note did not come back after recovery"; }
recovered_hash="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["content_hash"])' "$recovered")"
[ "$recovered_hash" = "$etag" ] || fail "the note changed across the outage"
ok "readiness is 200 again and the note is unchanged"

printf '\nsmoke passed\n'
