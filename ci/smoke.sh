#!/usr/bin/env bash
# The compose storyline, run against real images.
#
# It proves what the shipped slices claim: the stack comes up from a clean
# checkout with no manual setup, a note created through the API lands as a
# Markdown file with correct frontmatter in the notes filesystem, a write
# conditional on a stale ETag cannot overwrite an edit made on the volume
# while one carrying the current ETag lands, a note moved, renamed or deleted
# on the volume converges in the scheduled scan while the API keeps answering,
# readiness fails honestly when PostgreSQL is stopped, and everything recovers
# when it returns. Read from the volume, never through the API, whenever the
# claim is about a file.
#
# Usage:
#   bash ci/smoke.sh
#   COMPOSE_FILES="-f docker-compose.yml -f docker-compose.ci.yml" bash ci/smoke.sh
set -euo pipefail

COMPOSE_FILES="${COMPOSE_FILES:--f docker-compose.yml}"
# The editor container sits behind this profile so the quickstart does not
# start it. Every compose call here needs it, including `exec`.
export COMPOSE_PROFILES="${COMPOSE_PROFILES:-smoke}"
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

wait_for_note_path() {
    local note_id="$1" want="$2" tries="${3:-180}" response got code
    response="$(mktemp)"
    for _ in $(seq 1 "$tries"); do
        [ "$(status_of "$API/healthz")" = "200" ] \
            || fail "the API stopped answering during reconciliation"
        code="$(curl -sS -o "$response" -w '%{http_code}' "${AUTH[@]}" "$API/v1/notes/$note_id")"
        if [ "$code" = "200" ]; then
            got="$(field "$response" path)"
            [ "$got" = "$want" ] && return 0
        fi
        sleep 1
    done
    fail "note $note_id did not reconcile to $want"
}

wait_for_note_filter() {
    local note_id="$1" filter="$2" tries="${3:-180}" response code
    response="$(mktemp)"
    for _ in $(seq 1 "$tries"); do
        [ "$(status_of "$API/healthz")" = "200" ] \
            || fail "the API stopped answering during reconciliation"
        code="$(curl -sS -G -o "$response" -w '%{http_code}' "${AUTH[@]}" \
            --data-urlencode "$filter" "$API/v1/notes")"
        if [ "$code" = "200" ] && python3 -c 'import json,sys
items = json.load(open(sys.argv[1]))["items"]
raise SystemExit(0 if any(item["id"] == sys.argv[2] for item in items) else 1)' \
            "$response" "$note_id"; then
            return 0
        fi
        sleep 1
    done
    fail "note $note_id did not appear under filter $filter after one scan"
}

wait_for_adopted_path() {
    local path="$1" tries="${2:-180}" note_id code
    for _ in $(seq 1 "$tries"); do
        note_id="$(compose exec -T editor sed -n 's/^id: //p' "/data/notes/$path" | tr -d '[:space:]')"
        if [ -n "$note_id" ]; then
            code="$(status_of "${AUTH[@]}" "$API/v1/notes/$note_id")"
            [ "$code" = "200" ] && { printf '%s' "$note_id"; return 0; }
        fi
        sleep 1
    done
    fail "device-created note at $path was not adopted after it settled"
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

step "write a new note from the editor container and keep it in flight across a scan"
phone_path="Device/Made on phone.md"
# shellcheck disable=SC2016
compose exec -T editor sh -c \
    'mkdir -p /data/notes/Device; printf "%s\n" "# Made on phone" "" "First piece." > "$1"; touch /tmp/coppermind-writing; while test -e /tmp/coppermind-writing; do printf "%s\n" "Next piece." >> "$1"; sleep 10; done' \
    sh "/data/notes/$phone_path" >/dev/null 2>&1 &
writer_started=false
for _ in $(seq 1 30); do
    if compose exec -T editor test -e /tmp/coppermind-writing; then
        writer_started=true
        break
    fi
    sleep 1
done
[ "$writer_started" = true ] || fail "the device writer did not start"
scans_before="$(compose logs store 2>&1 | grep -c 'reconciliation completed' || true)"
for _ in $(seq 1 90); do
    scans_now="$(compose logs store 2>&1 | grep -c 'reconciliation completed' || true)"
    [ "$scans_now" -gt "$scans_before" ] && break
    sleep 1
done
[ "$scans_now" -gt "$scans_before" ] || fail "no scheduled scan ran while the note was in flight"
compose exec -T editor test -f "/data/notes/$phone_path" \
    || fail "the in-flight note disappeared"
# shellcheck disable=SC2016
compose exec -T editor sh -c '! grep -q "^id: " "$1"' sh "/data/notes/$phone_path" \
    || fail "the store adopted a device-created note before it settled"
compose exec -T editor rm /tmp/coppermind-writing
phone_id="$(wait_for_adopted_path "$phone_path")"
compose exec -T editor grep -Fqx "First piece." "/data/notes/$phone_path" \
    || fail "adoption changed the note content"
ok "the in-flight file stayed untouched, then became retrievable as $phone_id after quiet"

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
[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source"]["revision"])' "$ingested")" = "1" ] \
    || fail "the first ingest did not report source revision 1"
[ "$(python3 -c 'import json,sys; print(str(json.load(open(sys.argv[1]))["source"]["created"]).lower())' "$ingested")" = "true" ] \
    || fail "the first ingest did not report the source as created"
[ "$(python3 -c 'import json,sys; print(str(json.load(open(sys.argv[1]))["note"]["created"]).lower())' "$ingested")" = "true" ] \
    || fail "the first ingest did not report the note as created"
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

step "replay the identical source and prove nothing new is created"
source_before="$(compose exec -T store sh -c "find '/data/sources/$source_id' -type f -exec sha256sum {} \\; | sort")"
note_before="$(hash_on_volume "$ingest_note_path")"
duplicate="$(mktemp)"
code="$(curl -sS -o "$duplicate" -w '%{http_code}' -X POST "$API/v1/ingest" \
    "${AUTH[@]}" -H 'Content-Type: application/json' \
    --data-binary @examples/ingest/plaud-sample.json)"
[ "$code" = "200" ] || { cat "$duplicate"; fail "replay returned $code, expected 200"; }
[ "$(python3 -c 'import json,sys; print(str(json.load(open(sys.argv[1]))["source"]["created"]).lower())' "$duplicate")" = "false" ] \
    || fail "the replay did not report created false"
[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source"]["revision"])' "$duplicate")" = "1" ] \
    || fail "the replay did not report revision 1"
[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source"]["id"])' "$duplicate")" = "$source_id" ] \
    || fail "the replay returned a different source id"
[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["note"]["id"])' "$duplicate")" = "$ingest_note_id" ] \
    || fail "the replay returned a different note id"
[ "$(compose exec -T store sh -c 'find /data/sources -mindepth 1 -maxdepth 1 -type d | wc -l' | tr -d '[:space:]')" = "1" ] \
    || fail "the repeated external id created another source bundle"
[ "$(compose exec -T store sh -c 'find /data/notes/Review -maxdepth 1 -type f -name "*.md" | wc -l' | tr -d '[:space:]')" = "1" ] \
    || fail "the repeated external id created another Review note"
[ "$(compose exec -T store sh -c "find '/data/sources/$source_id' -type f -exec sha256sum {} \\; | sort")" = "$source_before" ] \
    || fail "the repeated external id changed the source bundle"
[ "$(hash_on_volume "$ingest_note_path")" = "$note_before" ] \
    || fail "the repeated external id changed the Review note"
ok "the replay returned the original ids with created false and one unchanged note"

step "edit the Review note, revise the source, and prove the note stays his"
compose exec -T store python3 -c \
    'import sys; p = sys.argv[1]; t = open(p).read(); t = t.replace("reviewed: false", "reviewed: true", 1); open(p, "w").write(t.replace("Target architecture agreed", "Captain corrected the note", 1))' \
    "/data/notes/$ingest_note_path"
reviewed_note="$(compose exec -T store cat "/data/notes/$ingest_note_path")"
reviewed_hash="$(hash_on_volume "$ingest_note_path")"
revised="$(mktemp)"
code="$(python3 -c 'import json,sys
document = json.load(open("examples/ingest/plaud-sample.json"))
document["source"]["artifacts"][0]["content"] = "Scott: corrected source content"
document["note"]["body"] = "This must not replace the reviewed note"
json.dump(document, sys.stdout)' | curl -sS -o "$revised" -w '%{http_code}' -X POST "$API/v1/ingest" \
    "${AUTH[@]}" -H 'Content-Type: application/json' --data-binary @-)"
[ "$code" = "200" ] || { cat "$revised"; fail "revision ingest returned $code, expected 200"; }
[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source"]["revision"])' "$revised")" = "2" ] \
    || fail "the changed source did not report revision 2"
[ "$(python3 -c 'import json,sys; print(str(json.load(open(sys.argv[1]))["source"]["created"]).lower())' "$revised")" = "true" ] \
    || fail "the changed source did not report a new revision"
[ "$(python3 -c 'import json,sys; print(str(json.load(open(sys.argv[1]))["note"]["created"]).lower())' "$revised")" = "false" ] \
    || fail "the changed source reported another note"
[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source"]["id"])' "$revised")" = "$source_id" ] \
    || fail "the revision returned a different source id"
[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["note"]["id"])' "$revised")" = "$ingest_note_id" ] \
    || fail "the revision returned a different note id"
compose exec -T store grep -Fq "Let's start with the architecture review" \
    "/data/sources/$source_id/r0001/transcript.txt" \
    || fail "revision 1 was changed or removed"
compose exec -T store grep -Fqx "Scott: corrected source content" \
    "/data/sources/$source_id/r0002/transcript.txt" \
    || fail "revision 2 does not carry the corrected source"
[ "$(hash_on_volume "$ingest_note_path")" = "$reviewed_hash" ] \
    || fail "the source revision changed the Review note"
[ "$(compose exec -T store cat "/data/notes/$ingest_note_path")" = "$reviewed_note" ] \
    || fail "the source revision reset the Review note content or state"
ok "revision 2 preserved revision 1 and the captain's reviewed note exactly"

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

step "create two more notes for listing and paging"
runbook_created="$(mktemp)"
code="$(curl -sS -o "$runbook_created" -w '%{http_code}' -X POST "$API/v1/notes" \
    "${AUTH[@]}" -H 'Content-Type: application/json' \
    -d '{"title":"Database Runbook","frontmatter":{"date":"2026-09-10","type":"note","context":"internal","tags":["runbook"]}}')"
[ "$code" = "201" ] || { cat "$runbook_created"; fail "runbook create returned $code, expected 201"; }
runbook_note_id="$(field "$runbook_created" id)"
runbook_note_path="$(field "$runbook_created" path)"
reference_created="$(mktemp)"
code="$(curl -sS -o "$reference_created" -w '%{http_code}' -X POST "$API/v1/notes" \
    "${AUTH[@]}" -H 'Content-Type: application/json' \
    -d '{"title":"Vendor Reference","frontmatter":{"date":"2026-09-20","type":"reference","context":"external","tags":["vendor"]}}')"
[ "$code" = "201" ] || { cat "$reference_created"; fail "reference create returned $code, expected 201"; }
reference_note_id="$(field "$reference_created" id)"
ok "four notes now exercise listing, filters and two-item pages"

step "read the file on the volume, not through the API"
on_disk="$(compose exec -T editor cat "/data/notes/$note_path")"
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
# From the editor container, never the store: the store is the process under
# test, so an edit made through it would prove only that it can change its own
# volume.
compose exec -T editor python3 -c \
    'import sys; p = sys.argv[1]; t = open(p).read(); open(p, "w").write(t.replace("reviewed: false", "reviewed: true", 1))' \
    "/data/notes/$note_path"
device_edit="$(compose exec -T editor cat "/data/notes/$note_path")"
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
[ "$(compose exec -T editor cat "/data/notes/$note_path")" = "$device_edit" ] \
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
on_disk="$(compose exec -T editor cat "/data/notes/$note_path")"
printf '%s\n' "$on_disk"
for line in "id: $note_id" "reviewed: true" "date: 2026-09-08" "- Corrected on review"; do
    printf '%s\n' "$on_disk" | grep -Fqx -- "$line" || fail "the replaced file is missing '$line'"
done
[ "$(hash_on_volume "$note_path")" = "$etag" ] || fail "the new ETag is not the hash of the file on the volume"
ok "the current write landed under the same identifier and path, with a fresh ETag"

step "list and filter notes from their current files"
wait_for_note_filter "$ingest_note_id" reviewed=true
reviewed="$(mktemp)"
code="$(curl -sS -o "$reviewed" -w '%{http_code}' "${AUTH[@]}" \
    "$API/v1/notes?reviewed=true")"
[ "$code" = "200" ] || { cat "$reviewed"; fail "reviewed listing returned $code, expected 200"; }
python3 -c 'import json,sys
actual = {item["id"] for item in json.load(open(sys.argv[1]))["items"]}
expected = set(sys.argv[2:])
raise SystemExit(0 if actual == expected else f"reviewed listing returned {actual}, expected {expected}")' \
    "$reviewed" "$ingest_note_id" "$note_id"
filtered="$(mktemp)"
code="$(curl -sS -o "$filtered" -w '%{http_code}' "${AUTH[@]}" \
    "$API/v1/notes?type=reference&context=external&tag=vendor")"
[ "$code" = "200" ] || { cat "$filtered"; fail "filtered listing returned $code, expected 200"; }
python3 -c 'import json,sys
items = json.load(open(sys.argv[1]))["items"]
assert len(items) == 1 and items[0]["id"] == sys.argv[2]' "$filtered" "$reference_note_id" \
    || fail "the filtered listing did not return only the vendor reference"
ok "filters see the reviewed device edit and select the expected subset"

step "page through four notes two at a time"
first_page="$(mktemp)"
code="$(curl -sS -o "$first_page" -w '%{http_code}' "${AUTH[@]}" \
    "$API/v1/notes?limit=2&folder=Review")"
[ "$code" = "200" ] || { cat "$first_page"; fail "first page returned $code, expected 200"; }
cursor="$(field "$first_page" next_cursor)"
[ -n "$cursor" ] && [ "$cursor" != "None" ] || fail "the first page did not carry a cursor"
second_page="$(mktemp)"
code="$(curl -sS -G -o "$second_page" -w '%{http_code}' "${AUTH[@]}" \
    --data-urlencode 'limit=2' --data-urlencode "cursor=$cursor" \
    --data-urlencode 'folder=Review' "$API/v1/notes")"
[ "$code" = "200" ] || { cat "$second_page"; fail "second page returned $code, expected 200"; }
python3 -c 'import json,sys
items = json.load(open(sys.argv[1]))["items"] + json.load(open(sys.argv[2]))["items"]
actual = [item["id"] for item in items]
expected = set(sys.argv[3:])
assert len(actual) == 4 and len(actual) == len(set(actual)) and set(actual) == expected
assert json.load(open(sys.argv[2]))["next_cursor"] is None' \
    "$first_page" "$second_page" "$ingest_note_id" "$note_id" "$runbook_note_id" "$reference_note_id"
ok "two pages returned all four notes exactly once"

step "move and rename a note on the volume, then find it by the same id"
reconciled_path="Work/Operations Runbook.md"
compose exec -T editor mkdir -p /data/notes/Work
compose exec -T editor mv "/data/notes/$runbook_note_path" "/data/notes/$reconciled_path"
wait_for_note_path "$runbook_note_id" "$reconciled_path"
ok "the scheduled scan followed the identity while the API kept answering"

step "delete that note on the volume and wait for an honest missing listing"
compose exec -T editor rm "/data/notes/$reconciled_path"
wait_for_note_filter "$runbook_note_id" "state=missing"
[ "$(status_of "${AUTH[@]}" "$API/v1/notes/$runbook_note_id")" = "404" ] \
    || fail "the deleted note still read as present"
ok "the deleted note reads as gone and lists as missing while the API keeps answering"

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
down_list="$(mktemp)"
code="$(curl -sS -o "$down_list" -w '%{http_code}' "${AUTH[@]}" "$API/v1/notes")"
[ "$code" = "503" ] || { cat "$down_list"; fail "a listing during the outage returned $code"; }
[ "$(field "$down_list" error)" = "metadata_unavailable" ] \
    || fail "a listing during the outage did not report metadata_unavailable"
ok "readiness and listing are 503, and mutations return a clean metadata_unavailable"

step "the notes filesystem is untouched by the outage"
still_there="$(compose exec -T store cat "/data/notes/$note_path")"
[ "$still_there" = "$on_disk" ] || fail "the note file changed during the outage"
[ "$(review_count)" = "$before_outage" ] \
    || fail "the refused write left a file behind in the review folder"
ok "the note remains readable on the filesystem and the refused write left no orphan file"

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
