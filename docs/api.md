# Coppermind API surface (approved plan)

This is the full interface surface from the approved plan: every route,
every scope, and how errors are shaped, so someone could build a client or
rebuild the service from this document alone if they had to. It is the
design as approved, not a status report; what is actually live today is
[STATUS.md](../STATUS.md), and [roadmap.md](roadmap.md) says what is next.
The story behind these decisions is [architecture.md](architecture.md).

## Conventions that hold across every route

- Base path `/v1`. Authentication is `Authorization: Bearer cm_<key_id>_<secret>`.
  The `cm_` prefix exists so an automated secret scanner can recognize a
  leaked key on sight. Keys are hashed (argon2) and shown exactly once, at
  creation.
- Every route needs a scope: `sources:read`, `sources:write`, `notes:read`,
  `notes:write`, `notes:move`, `notes:delete`, `journal:read`,
  `journal:write`, `search:read`, `admin:read`, `admin:write`. A key missing
  the right scope gets 403, not a filtered response.
- Admin's own pages use a signed session cookie; its `/v1/admin/*` API
  endpoints accept either that cookie or a key holding an `admin:*` scope.
- Every non-2xx response is the same shape: `{"error": "<code>", "message":
  "<human text>", ...extra fields the specific error needs}`.
- Listing endpoints page with an opaque cursor: `?cursor=<opaque>&limit=<1 to
  200, default 50>`, returning `{"items": [...], "next_cursor": "..." or
  null}`. The cursor is built from permanent note identities, so paging is
  stable across a rename that happens between two pages.

### Error codes

| Code | HTTP status | Meaning |
|---|---|---|
| `validation_error` | 422 | The request body or a field failed validation |
| `unauthorized` | 401 | Missing or invalid API key |
| `forbidden` | 403 | Valid key, wrong scope |
| `not_found` | 404 | No such note, source, or resource |
| `version_conflict` | 409 | `If-Match` did not match the current file; carries `current_version` |
| `precondition_required` | 428 | A write that requires `If-Match` was sent without it |
| `path_collision` | 409 | The target path already exists; carries `existing_path` |
| `payload_too_large` | 413 | Over a configured size limit; carries `limit_bytes` |
| `gone` | 410 | The resource existed but was deliberately removed (a tombstoned source) |
| `method_not_allowed` | 405 | A mutating verb against a read-only resource, such as any write to `/v1/sources/**` |
| `metadata_unavailable` | 503 | PostgreSQL is down and the operation needs it (listing, search, Admin mutations) |
| `store_unavailable` | 503 | The store service itself cannot be reached |
| `already_running` | 409 | A job of the same kind is already in progress |

## Notes and folders

| Method and path | Scope | What it does | Success | Failure |
|---|---|---|---|---|
| `POST /v1/notes` | `notes:write` (also `journal:write` when `type: journal`) | Create a note from `{title, body, frontmatter{}, folder?}` | 201, note document, `ETag` header | 422; 409 `path_collision` |
| `GET /v1/notes/{id}` | `notes:read` | Read a note. `Accept: application/json` returns the document shape; `text/markdown` returns the raw file | 200, `ETag: "sha256:<bytes hash>"` | 404 |
| `PUT /v1/notes/{id}` | `notes:write` | Replace a note's frontmatter and body. Requires `If-Match` | 200, new `ETag` | 404; 409 `version_conflict`; 428 without `If-Match` |
| `PATCH /v1/notes/{id}/frontmatter` | `notes:write` | Change named frontmatter fields only, via `{set:{...}, unset:[...]}`. Setting `reviewed: true` is how "mark reviewed" works | 200 | 404; 409; 422 for a value outside the shipped vocabulary |
| `GET /v1/notes` | `notes:read` | List and filter by `folder, reviewed, type, context, account, from, to, tag, state` | 200, page of summaries | 503 `metadata_unavailable` if PostgreSQL is down, never a silently empty page |
| `POST /v1/notes/{id}/move` | `notes:move` | Move to another folder; `If-Match` optional | 200, `{path}` | 404; 409 `path_collision`; 422 |
| `POST /v1/notes/{id}/rename` | `notes:write` | Rename the note's title; `If-Match` required. Links to it are not rewritten, and that gap is deliberate, not a bug | 200, `{path}` | 404; 409 |
| `DELETE /v1/notes/{id}` | `notes:delete` | Move the note to the trash folder; `If-Match` required | 200, `{trash_path}` | 404; 409 |
| `GET /v1/notes/{id}/sources` | `notes:read`, `sources:read` | List the sources a note cites | 200 | 404 |
| `GET /v1/folders` | `notes:read` | The folder tree with note counts | 200 | |

## Sources (read-only by design)

Sources are immutable once ingested; nothing here ever changes one after the
fact, which is why every mutating verb on a source route is refused rather
than routed anywhere.

| Method and path | Scope | What it does | Success | Failure |
|---|---|---|---|---|
| `POST /v1/ingest` | `sources:write`, `notes:write` | Create a source bundle and its opening note together, or neither | 201 on first creation; 200 `created: false` on an identical replay; 200 with a new `source.revision` when content changed | 422; 413 over the ingest size limit |
| `GET /v1/sources` | `sources:read` | List sources, filterable by `provider, from, to` | 200 | |
| `GET /v1/sources/{id}` | `sources:read` | The source manifest | 200 | 404; 410 if tombstoned |
| `GET /v1/sources/{id}/revisions/{n}/artifacts/{name}` | `sources:read` | Stream one artifact, with its recorded MIME type | 200 | 404 |
| `GET /v1/sources/{id}/projection` | `sources:read` | The generated Markdown page for the latest revision | 200 `text/markdown` | 404 |
| `PUT`, `PATCH`, `DELETE` on any `/v1/sources/**` path | | Always refused; source integrity is a design invariant, not a permission you can be granted | | 405 `method_not_allowed` |

## Search, attachments, and status

| Method and path | Scope | What it does | Success | Failure |
|---|---|---|---|---|
| `GET /v1/search` | `search:read` | Full-text search with `q` (websearch syntax), `reviewed_only`, `include_unreviewed`, and the same filters as listing | 200, page including `rank` and a `snippet`; every item carries `reviewed` | 503 `metadata_unavailable` |
| `POST /v1/attachments` | `notes:write` | Upload a file (multipart); name comes from the filename | 201, `{path, embed: "![[name.png]]", size_bytes}` | 413 over the plan's file-size limit; 409 |
| `GET /v1/attachments/{name}` | `notes:read` | Download an attachment | 200 | 404 |
| `GET /v1/status` | any key | Version, capabilities, note and job counters, and per-helper `{last_success_at, age_s, ok}` | 200 | |
| `GET /healthz`, `GET /readyz`, `GET /metrics`, `GET /openapi.json` | none | Process liveness, real readiness (checks PostgreSQL and the notes filesystem), Prometheus metrics, the generated API contract | 200 | 503 on `/readyz` when a real dependency is down |

## Admin: pages and their endpoints

Admin is server-rendered; every page below has a matching JSON endpoint.
Every product setting that exists gets a page and control here, with a
working default already in place, per the standing rule that nothing ships
requiring hand-populated configuration.

| Page | Endpoints | What it is for |
|---|---|---|
| Claim (first boot) | `POST /v1/admin/claim {code, password}` | Set the admin password using the one-time code the store logged and wrote to `/data/state/internal/claim-code` |
| Login, logout | `POST /v1/admin/login`, `POST /v1/admin/logout` | Password-backed session |
| Overview | `GET /v1/admin/status` | Counters, helper status and ages, last reconcile, last commit, last sync, failed jobs |
| API keys | `GET/POST /v1/admin/keys`, `DELETE /v1/admin/keys/{key_id}` | Create (secret shown once), list, and revoke keys graphically instead of through the store's command line |
| Obsidian Sync | `GET /v1/admin/sync`, `POST /v1/admin/sync/connect`, `POST /v1/admin/sync/token`, `POST /v1/admin/sync/pause`, `/resume`, `/disconnect` | Connect with email, password, optional MFA, and a vault name or a pasted token; reveal a token once, with a hint for sealing it into GitOps; pause and resume around bulk operations |
| Settings | `GET/PUT /v1/admin/settings {if_revision, body}` | Every key in the settings table below, grouped by section |
| Frontmatter schema | `GET/PUT /v1/admin/schema`, `POST /v1/admin/schema/rename-key {from, to}` | Keys, kinds, and vocabularies; renaming a key runs the migration job that rewrites every note through the store |
| Filing rules | `GET/PUT /v1/admin/rules`, `POST /v1/admin/rules/preview {note_id}` | The ordered rules a note is filed by, with a dry run ("what would this note file as") before committing to a change |
| Jobs | `GET /v1/admin/jobs`, `POST /v1/admin/jobs {kind}`, `GET /v1/admin/jobs/{id}` | Run reconcile, a full rehash, rebuild metadata from disk, reindex, or rebuild projections, and see the history of each |
| Sources | `GET /v1/admin/sources`, `GET /v1/admin/notes/problems` | Browse sources; see notes the reconciler could not parse or file |

## The internal contract behind the public one

The API never touches a file directly. It calls a narrow, typed internal
contract that the store alone implements, over `Authorization: Bearer
<internal token>` on port 8081, prefix `/internal/v1`. The same contract has
two implementations behind one interface, an in-process one and an HTTP
client, so the shape cannot silently drift between how the store checks its
own behavior and how everything else calls it.

The calls it exposes, grouped by what they touch:

- **Notes:** create, get, read raw content, list with filters, replace
  (conditional on `If-Match`), patch frontmatter, move, rename, delete
  (to trash), list folders.
- **Sources:** ingest (idempotent), get one, list, open one artifact,
  list the sources a note cites.
- **Attachments:** store one, open one.
- **Control state:** get and put a named state file (`settings`, `schema`,
  `rules`, `keys`, `admin`), each guarded by the revision the caller last
  read.
- **Jobs and status:** start a job by kind, check a job's progress, read
  overall store status.

Typed failures a caller has to handle regardless of which implementation it
is talking to: not found, a version conflict carrying the current ETag, a
revision conflict on a control-state write, an attempt to mutate an
immutable source, a path collision, a validation failure, a payload over the
limit, and metadata unavailable when PostgreSQL cannot answer.

The write protocol every mutation follows, in order: validate and compute
the target path; write to a temporary file in the target directory, make it
durable, then rename it into place; hash the final bytes; in one PostgreSQL
transaction, update the metadata mirror and record an outbox event; commit.
A crash at or after the file write leaves the filesystem as the truth, and
the next reconciliation pass brings PostgreSQL back in step, never a client
retry that could create a duplicate.

## Edge cases worth knowing before integrating against this

- **Duplicate identifiers.** Duplicating a note in Obsidian copies its ID
  too. The file at the path the database last recorded keeps that ID; any
  other file carrying the same one gets a fresh ID written back once it
  settles, and Admin is notified.
- **A note's identifier changes or disappears from a known path.** The file
  is always the truth, so the old note reads as deleted and a new one
  appears; nothing is silently "repaired", and both are surfaced together in
  Admin so the pair can be reconciled by a person.
- **Unparseable files are never rewritten.** Malformed frontmatter, a file
  that is not valid Markdown, or content that will not decode gets recorded
  as `unparsed` with a reason, and its body is still indexed if it can be
  decoded at all.
- **A move never breaks a link; a rename can.** Obsidian resolves
  `[[wikilinks]]` by filename, so the curator only ever moves a note
  between folders and never renames one. The rename endpoint exists for a
  person to use deliberately, and it documents that link rewriting is not
  part of it.
