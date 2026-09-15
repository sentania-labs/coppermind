# Coppermind

A filesystem-first personal knowledge platform. Notes are Markdown files with
YAML frontmatter in a plain directory tree, the notes filesystem. Obsidian
Sync carries that tree to a phone and a laptop, Git keeps its history, and a
small set of services put things into it, file them and make them findable.
Nothing in Coppermind owns your notes in a way you could not walk away from:
the files are the product, and everything else is rebuildable from them.

The design contract is the decision records in
[docs/decisions](docs/decisions): what was chosen, what was rejected, and why.

## Quickstart

```bash
docker compose up -d
```

That is the whole setup. A one-shot bootstrap container creates the volumes,
generates the internal credentials and a default API key, and writes the
settings, frontmatter schema and API key hash with working defaults. Nothing
has to be hand populated before the stack runs. Read the default key from its
restricted bootstrap volume into the current shell:

```bash
COPPERMIND_KEY="$(docker compose run --rm --no-deps --entrypoint cat bootstrap \
  /run/coppermind/api/default-api-key)"
```

Revoke that default once you have minted your own key. A revoked default is
never minted again, and it stops authenticating as soon as the API's cache
next loads, but the file above still holds the dead credential until the next
`docker compose up` replaces it with a sentence saying it was revoked.

Bootstrap mints the default once and never a second time. Lose the
`default-api-key` volume while the default is still live and that credential
is gone for good: the next start reports it unrecoverable, leaves the record
as it is rather than minting a replacement that would leave the first one
live, and puts a sentence in the file telling you to run the keys command
below.

Create a note:

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/notes \
  -H "Authorization: Bearer $COPPERMIND_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
        "title": "Ameren Architecture Sync",
        "body": "## Key points\n- Target architecture agreed\n",
        "frontmatter": {"date": "2026-09-08", "type": "meeting",
                        "context": "customer", "account": "Ameren"}
      }'
```

The answer carries the note's permanent identifier and its path. That path is
a real file on the `data` volume:

```bash
docker compose exec store cat "/data/notes/Review/2026-09-08 Ameren Architecture Sync.md"
```

Read it back as a document:

```bash
curl -sS -H "Authorization: Bearer $COPPERMIND_KEY" \
  http://127.0.0.1:8080/v1/notes/<id>
```

List notes and narrow them by folder, reviewed state, type, context, account,
date bounds or tag. Each summary comes from the latest scheduled filesystem
scan and carries its observed `state`: `ok`, `unparsed` or `missing`. Pass the
answer's `next_cursor` back to get the following page. Cursors use permanent
note identities, so a rename does not change where paging resumes:

```bash
curl -sS -G -H "Authorization: Bearer $COPPERMIND_KEY" \
  --data-urlencode 'folder=Review' \
  --data-urlencode 'reviewed=false' \
  --data-urlencode 'limit=25' \
  http://127.0.0.1:8080/v1/notes
```

A filter sent with an empty value answers 422 `validation_error` rather than an
empty page, so an unset field is never mistaken for a real result.

Edit that note: send the document back with `If-Match` carrying the `ETag` the
read returned. Without the header the answer is 428. If the file changed since
the read, on any device, the answer is 409 `version_conflict` naming the
current ETag and nothing is written. Send both `frontmatter` and `body`:
leaving either out answers 422 `validation_error` and the note is unchanged.

```bash
curl -sS -X PUT http://127.0.0.1:8080/v1/notes/<id> \
  -H "Authorization: Bearer $COPPERMIND_KEY" \
  -H 'Content-Type: application/json' \
  -H 'If-Match: "sha256:<the ETag the read returned>"' \
  -d '{
        "frontmatter": {"schema_version": 1, "date": "2026-09-08", "type": "meeting",
                        "context": "customer", "account": "Ameren", "reviewed": true,
                        "sources": [], "tags": []},
        "body": "# Ameren Architecture Sync\n\n## Key points\n- Target architecture agreed\n- Corrected on review\n"
      }'
```

Bring something in from outside. An ingest posts a source and the note to
open for it, and creates both or neither. `examples/ingest/plaud-sample.json`
is a recorder's transcript, its summary and its device metadata:

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/ingest \
  -H "Authorization: Bearer $COPPERMIND_KEY" \
  -H 'Content-Type: application/json' \
  --data-binary @examples/ingest/plaud-sample.json
```

The answer carries the new source identifier and the Review note that cites
it. The artifacts are files beside the manifest:

```bash
docker compose exec store ls -R "/data/sources/<source_id>"
```

Sending the same payload a second time answers 200 with `created: false` and
the original identifiers. Changing an artifact appends a numbered source
revision without rewriting the Review note. Changing only a field that
describes the source, such as `captured_at` or an artifact's `mime_type`,
while the artifacts stay the same is still a replay: keeping that correction
is not built yet, so the answer names the fields in `source.unstored_fields`
instead of discarding them quietly. A source remains keyed by its `provider`
and `external_source_id`. A body over `limits.ingest_max_bytes` answers 413
`payload_too_large`. The API measures the submitted body before forwarding
its parsed request, but only after FastAPI has buffered it, so the refusal
does not reduce memory use. The key needs both `sources:write` and
`notes:write`.

The generated OpenAPI document is at `http://127.0.0.1:8080/openapi.json`.

Until the separate Admin service adds its graphical keys page, additional
keys and rotations use the Store command. It prints a new credential once and
keeps only its hash. Grant one or more of the scopes shown by `--help`, move
callers to it, then revoke the old key by its id:

```bash
docker compose exec store python3 -m coppermind_store.keys create \
  --name automation --scope notes:read --scope notes:write
docker compose exec store python3 -m coppermind_store.keys revoke <key_id>
```

Nobody has to touch Git for the notes filesystem to have a history. A few
minutes after a change settles, it is a commit:

```bash
docker compose exec git git -C /data/notes log --stat
```

The Obsidian Sync helper is supervised and answers its control endpoint, but
it does not sync to a device yet. Connecting a real account is refused on
purpose: how the first connection should behave, and where the account
credential should live, are both open decisions, so nothing here touches an
Obsidian account or a remote vault object. The refusal is what you get back.

```bash
docker compose exec obsidian-sync node /app/control.mjs status
docker compose exec obsidian-sync node /app/control.mjs connect "My Remote Vault"
# 501 real_sync_refused
```

What the status does and does not say:

- `simulated` is true only when the CI overlay is running the bundled stand-in
  client; `real_sync_supported` is false everywhere today.
- `sync_mode` and `conflict_strategy` stay null until a client reports them.
- `liveness` is `child_process_only`: the supervisor watches the client
  process, so it cannot tell a running client from a delivering one.

Pause and resume use the same internal control endpoint through the packaged
command. Both are refused for the same reason connect is, so nothing can
persist a paused connection that would read as a working one:

```bash
docker compose exec obsidian-sync node /app/control.mjs pause
docker compose exec obsidian-sync node /app/control.mjs resume
```

## What is running

| Service | Does | State |
|---|---|---|
| `api` | the public contract on `:8080` | five-minute key cache; no durable state |
| `store` | the only process that writes the notes filesystem | `/data`, one replica always |
| `git` | records the history of the notes filesystem; no network, no credential | `/data/notes/.git`, one replica always |
| `obsidian-sync` | supervises the sync client and exposes internal lifecycle control; real sync refused for now | `/data/state/sync`, one replica always |
| `postgres` | mirrored and derived state, rebuildable from `/data` | `pgdata` volume |
| `bootstrap`, `migrate` | one-shot, run on every `up` and exit | none |

Health is honest. `/healthz` says the process is up; `/readyz` says the
service can do its job. Stop PostgreSQL and `/readyz` answers 503 and says
why, note writes, reads and listings answer 503 `metadata_unavailable`, and
the notes filesystem itself carries on unaffected. Start it again and API
operations recover on their own, with no restart and nothing to clear by hand.
A note edited in place while PostgreSQL was down reads back as soon as it
returns, because a by-ID read parses the current file. The store scans the
notes filesystem in the background every 60 seconds by default. Known notes
edited, moved, renamed or deleted on a device converge in the next scan, while
the API continues answering. A note reads as `missing` only when the scan did
not find its file; one it can see but cannot parse or open reads as `unparsed`
instead. A file created on a device remains untouched and unknown until
write-side reconciliation lands.

## Working on it

```bash
make setup            # uv workspace: shared package plus the services
make check            # lint, types, unit tests, compose validity, house rules
make db-up test-integration db-down   # the PostgreSQL backed tests
make scan             # dependency, secret and repository scans, as CI runs them
make image            # build the images locally
make smoke            # the compose storyline end to end
make failure          # helpers stopped and started with edits in between
make sync-smoke       # simulated sync connect, pause, resume and restart (needs the CI overlay)
```

[CONTRIBUTING.md](CONTRIBUTING.md) has the bar for a pull request.
[STATUS.md](STATUS.md) says what works today and what does not exist yet.

## Vocabulary

The tree of Markdown files is the **notes filesystem**. The word "vault"
appears only for Obsidian's own remote vault object, which is a thing on
Obsidian's servers, not a thing here.
