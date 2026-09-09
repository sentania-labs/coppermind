# Coppermind

A filesystem-first personal knowledge platform. Notes are Markdown files with
YAML frontmatter in a plain directory tree, the notes filesystem. Obsidian
Sync carries that tree to a phone and a laptop, Git keeps its history, and a
small set of services put things into it, file them and make them findable.
Nothing in Coppermind owns your notes in a way you could not walk away from:
the files are the product, and everything else is rebuildable from them.

Architecture specification v0.3 is the design contract. The decisions that
shaped it are in [docs/decisions](docs/decisions).

## Quickstart

```bash
docker compose up -d
```

That is the whole setup. A one-shot bootstrap container creates the volumes,
generates the credentials the services use between themselves, and writes the
settings and frontmatter schema with working defaults, so nothing has to be
hand populated before the stack runs.

Create a note:

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/notes \
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
curl -sS http://127.0.0.1:8080/v1/notes/<id>
```

The generated OpenAPI document is at `http://127.0.0.1:8080/openapi.json`.

## What is running

| Service | Does | State |
|---|---|---|
| `api` | the public contract on `:8080` | none; it calls the store |
| `store` | the only process that writes the notes filesystem | `/data`, one replica always |
| `postgres` | mirrored and derived state, rebuildable from `/data` | `pgdata` volume |
| `bootstrap`, `migrate` | one-shot, run on every `up` and exit | none |

Health is honest. `/healthz` says the process is up; `/readyz` says the
service can do its job. Stop PostgreSQL and `/readyz` answers 503 and says
why, note writes and reads answer 503 `metadata_unavailable`, and the notes
filesystem itself carries on unaffected. Start it again and everything
converges without intervention.

## Working on it

```bash
make setup            # uv workspace: shared package plus both services
make check            # lint, types, unit tests, compose validity, house rules
make db-up test-integration db-down   # the PostgreSQL backed tests
make scan             # dependency, secret and repository scans, as CI runs them
make image            # build both images locally
make smoke            # the compose storyline end to end
```

[CONTRIBUTING.md](CONTRIBUTING.md) has the bar for a pull request.
[STATUS.md](STATUS.md) says what works today and what does not exist yet.

## Vocabulary

The tree of Markdown files is the **notes filesystem**. The word "vault"
appears only for Obsidian's own remote vault object, which is a thing on
Obsidian's servers, not a thing here.
