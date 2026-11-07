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
  `notes:write`, `notes:move`, `notes:delete`, `artifacts:read`,
  `artifacts:write`, `journal:read`, `journal:write`, `search:read`,
  `admin:read`, `admin:write`, `admin:purge`, `entities:read`,
  `entities:write`. A key missing the right scope gets 403, not a filtered
  response. The bootstrap default key and a key created with no `--scope` hold
  every scope except `admin:purge`, `entities:read` and `entities:write`,
  which are granted only when named. Keys minted before the artifact scopes
  existed do not gain them; mint a key that holds them.
- Admin's own pages use a signed session cookie; its `/v1/admin/*` API
  endpoints accept either that cookie or a key holding an `admin:*` scope.
- Every non-2xx response is the same shape: `{"error": "<code>", "message":
  "<human text>", ...extra fields the specific error needs}`.
- Listing endpoints page with an opaque cursor: `?cursor=<opaque>&limit=<1 to
  200, default 50>`, returning `{"items": [...], "next_cursor": "..." or
  null}`. Note cursors use permanent identities, so paging is stable across renames.
  Source cursors use the first-ingested timestamp and source identity. Invalid
  source cursors, including timestamps without a UTC offset, return 422
  `validation_error`. Search cursors resume after the last hit by rank and
  then by kind and identifier, and are tied to the `q` they paged: sending one
  with a different `q` answers 422 `validation_error`. An edit that changes a
  note's rank between pages can move it across the page boundary.

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
| `metadata_unavailable` | 503 | PostgreSQL is down and the operation needs it. That covers listing, search, and Admin mutations, but also an ordinary note read, create, or replace by ID: resolving a note's path goes through the mirror, so those stop too. Only a file edited directly on the notes filesystem, outside the API, keeps working |
| `store_unavailable` | 503 | The store service itself cannot be reached |
| `already_running` | 409 | A job of the same kind is already in progress |

## Dossiers

`GET /v1/entities` and `GET /v1/entities/{id}` require `entities:read` and
`notes:read`, because dossiers quote reviewed notes. `POST /v1/entities` and
`POST /v1/entities/{id}` additionally require `entities:write`. Changes carry
the current integer `version`. See the
[dossier contract](dossiers.md#agent-api) for filters, corrections, evidenced
claims, and merge/split previews. No existing API key gains these scopes.

Entity search hits require both `notes:read` and `entities:read`. Unauthorized
kinds are excluded before retrieval, ranking and pagination. Entity hits have `kind: entity` and
link to stable `People/` or `Companies/` pages. Dossier permissions never grant
access to source artifacts.

## Notes and folders

| Method and path | Scope | What it does | Success | Failure |
|---|---|---|---|---|
| `POST /v1/notes` | `notes:write` (also `journal:write` when `type: journal`) | Create a note from `{title, body, frontmatter{}, folder?}` | 201, note document, `ETag` header | 422; 409 `path_collision` |
| `GET /v1/notes/{id}` | `notes:read` | Read a note. `Accept: application/json` returns the document shape; `text/markdown` returns the raw file | 200, `ETag: "sha256:<bytes hash>"` | 404 |
| `PUT /v1/notes/{id}` | `notes:write` | Replace a note's frontmatter and body. Requires `If-Match` | 200, new `ETag` | 404; 409 `version_conflict`; 428 without `If-Match` |
| `PATCH /v1/notes/{id}/frontmatter` | `notes:write` | Change named frontmatter fields only, via `{set:{...}, unset:[...]}`. Setting `reviewed: true` is how "mark reviewed" works | 200 | 404; 409; 422 for a value outside the shipped vocabulary |
| `GET /v1/notes` | `notes:read` | List and filter by `folder, reviewed, type, context, account, from, to, tag, state`. `account` matches a note whose `account` list has that name among its entries. `from` and `to` are inclusive days (`YYYY-MM-DD`) taken from the note's `date`, whether it is a date or a date-and-time | 200, page of summaries | 503 `metadata_unavailable` if PostgreSQL is down, never a silently empty page |
| `POST /v1/notes/{id}/move` | `notes:move` | Move to another folder, keeping the filename; `If-Match` optional. A folder that differs from an existing one only by case takes its spelling, and the current folder is a no-op | 200, `{path}` | 404; 409 `path_collision`; 422 |
| `POST /v1/notes/{id}/rename` | `notes:write` | Rename the note's title; `If-Match` required. Links to it are not rewritten, and that gap is deliberate, not a bug | 200, `{path}` | 404; 409 |
| `DELETE /v1/notes/{id}` | `notes:delete` | Move the note to the trash folder; `If-Match` required | 200, `{trash_path}` | 404; 409 |
| `GET /v1/notes/{id}/sources` | `notes:read`, `sources:read` | List the sources a note cites, each as `{id, projection_path}` with the projection page its manifest records; citations come from the current mirrored frontmatter | 200, a list | 404 for an unknown note; 503 `metadata_unavailable` |
| `GET /v1/folders` | `notes:read` | The folder tree with note counts | 200 | |

## Declared frontmatter filters

`GET /v1/notes` and `GET /v1/search` accept an optional URL-encoded JSON
`filter` parameter. For example, send this JSON as the parameter value:

```json
{"all":[{"field":"attendees","op":"contains","value":"Alice"},{"field":"date","op":"gte","value":"2026-10-01"}]}
```

The field names must exist in the current schema. All clauses and all legacy
filters are combined with AND. At most 20 clauses and 8 KiB of URL-encoded JSON
are accepted. Strings and enums use case-sensitive `eq`; booleans and integers
use typed `eq` without coercion. Dates use canonical `YYYY-MM-DD` with `eq`,
`gte`, or `lte` (inclusive). Lists use `contains` for exact element membership,
not substring matching. Multiple clauses on a list require every requested
member. List elements may be JSON values because the schema does not restrict
their element type. Top-level null, unknown fields, invalid operators or types,
and reversed date ranges return 422. Missing, null, and invalid stored values
never match. No note body or frontmatter is rewritten by filtering or backfill.

Declaring or changing a field automatically schedules a typed-index rebuild.
Until it finishes, queries involving that field return 503
`field_index_not_ready` with a `job_id`, never misleading empty results.
Unchanged ready fields remain queryable. Admin's **Field index** page shows
per-field readiness, progress, failed rebuilds, and paginated paths with invalid
values. Rebuild retries a failed job or reconstructs this derived index. Requests made
while a refresh is running are persisted for the next pass and survive restart. The
worker checks for schema changes every ten seconds. A rebuild uses a database
write barrier of up to 30 seconds plus a two-second lock acquisition timeout;
metadata writes can wait, while readers retain the previous committed index.
A timeout rolls back the rebuild and requires an Admin retry. This is a bounded
full-generation rebuild, not a general job scheduler or an unlimited import job.

Filtered cursors bind the normalized clause order, all other filters, and the
schema revision. Changing these returns 409 `filter_cursor_conflict`: restart
from the first page. Page size may change. Listing retains immutable-ID ordering
through renames; pages reflect live data rather than a frozen export. Existing
listing state rules remain unchanged, including explicitly queried missing or
deleted notes. Search retains its reviewed/filed gate; a custom filter matches
notes only, never generated source pages.

The internal Admin calls are `POST /internal/v1/field-index/status?offset=0`
and `POST /internal/v1/field-index/rebuild` (202). They require the internal
token. `/admin/field-index` and its rebuild form require an Admin session and
CSRF token. Index rows are maintained in the same transaction as mirror writes;
metadata reconstruction repopulates them. Migration 0009 adds only derived
tables and a trigger. Its downgrade removes those objects without changing notes.

## Schema

| Method and path | Scope | What it does | Success | Failure |
|---|---|---|---|---|
| `GET /v1/schema` | `notes:read` | The rules every write is held to, as the store reads them from `/data/state/schema.yaml`: `revision`, `schema_version`, `fields[]` (each with `name`, `kind`, `required`, `required_when {key, values}` or null, `default`, `description`, `guidance`, and `allowed_values[] {value, meaning}`, empty when any value of the kind is accepted), the `roles` map, and `tags {mode, listed[] {tag, meaning, aliases[]}, aliases {alias: tag}}`. Agents and the enricher read field and tag guidance here | 200 | 503 `store_unavailable`; 500 when the schema file cannot be read, never a bundled default in its place |

Tags follow these rules on every store write: create, ingest, replace,
frontmatter patch, and the reconciler's adoption of a device-created note.
An alias is replaced by its canonical tag and a repeated tag is dropped,
keeping the order the caller or person gave; a list with neither is written
as sent. With `mode: open` (the default) any tag is accepted. With
`mode: closed` a write that adds a tag which is neither listed nor an alias of
a listed tag answers 422 `validation_error`; a tag the note already carries is
never refused, and adoption never refuses a device-created note over its tags.
Adoption rewrites only the tag lines of the file, leaving every other byte and
line ending as it was, and leaves a tag list with a comment inside it as the
person wrote it.

For workflow callers, see [n8n intake](n8n-intake.md) for the current JSON
contract and the separately labeled staged-upload proposal. Review and filing
requirements are recorded in [decision 002](decisions/002-review-and-organization.md).

Note documents and list items include `filed`, `filing_error` (the last error,
otherwise null), and `filing_attempts` (completed or failed filing attempts,
excluding checks of already-filed notes). Filing errors count only reviewed
notes still present; successful retries clear them. Status includes
`filing_queue_depth` for reviewed, present notes awaiting filing, including errors.
`filing_ready_depth` excludes notes currently failing. `filing_estimated_drain_s`
applies only to that ready work and is null when progress has stalled or no
measurement exists. `filing_last_progress_at` records the latest successful filing;
`filing_stalled` signals a ready queue with no recent progress. Problems names each
failed note, its attempt count, and a plain explanation with a suggested remedy.

Reads follow a moved note's identity when its recorded path is stale. An `ok`
listing omits files that have disappeared or cannot be parsed until the next
reconciliation records their state. A file that exists but cannot be read is also
left out, paging continues past it, and Problems names the note (`unreadable_note`)
until it can be read again. If the notes filesystem as a whole cannot be read, the
listing fails with the standard `503` `notes_filesystem_unavailable` error, which
can be retried once the volume is healthy, instead of returning a partial page.
Problems includes missing notes with their `state_reason` and durable path
notices naming the observed and indexed paths.
While filing is in flight, its journal holds hash-checked note copies; they are
dropped once filing settles, and a retention purge removes a purged note's
journal. When a note vanishes between paths, marking it deleted requires Git
evidence of removal of a journaled origin; otherwise the missing reason names
the last known hash and why evidence is unavailable. Recovery can preview and
explicitly restore a missing note from its verified matching filing copy without
claiming a committed removal (`evidence_pending: true`, empty `removal_commit`).
Confirmation is bound to the note ID, destination and content hash, and restore
never overwrites a file. When no matching copy exists, Admin explains how to
restore the original file and ID from a device or backup. A path notice
reports a disagreement that is still open and clears when the index and the file
agree again or the note is gone.

## Sources (read-only by design)

Sources are immutable once ingested; nothing here ever changes one after the
fact, which is why every mutating verb on a source route is refused rather
than routed anywhere.

| Method and path | Scope | What it does | Success | Failure |
|---|---|---|---|---|
| `POST /v1/ingest` | `sources:write`, `notes:write` | Create a source bundle and its opening note. The files land first; if the database commit that follows fails, the completed bundle, note, and claim are kept rather than rolled back, and a retry repairs the mirror without duplicating anything | 201 on first creation; 200 `created: false` on an identical replay (the supported way to retry safely, not a failure); 200 with a new `source.revision` when content changed | 422; 413 over the ingest size limit; 503 if the database commit fails after the files are written, resolved by retrying |
| `GET /v1/sources` | `sources:read` | List sources newest first, filterable by `provider` and by `from`, `to` (inclusive dates the source was first ingested), paged with `cursor` and `limit` | 200, page of `{id, provider, external_source_id, source_type, origin, current_revision, created_at}` | 422 for a bad filter or cursor; 503 `metadata_unavailable`, never an empty page |
| `GET /v1/sources/{id}` | `sources:read` | The source manifest | 200 | 404; 410 if tombstoned |
| `GET /v1/sources/{id}/revisions/{n}/artifacts/{name}` | `sources:read` | Stream one artifact, with its recorded MIME type | 200 | 404 |
| `GET /v1/sources/{id}/projection` | `sources:read` | The generated Markdown page for the latest revision | 200 `text/markdown` | 404 |
| `PUT`, `PATCH`, `DELETE` on any `/v1/sources/**` path | | Always refused; source integrity is a design invariant, not a permission you can be granted | | 405 `method_not_allowed` |

Refused ingests are persisted as individual records under
`/data/state/rejections/` before their database mirror is updated. Reconciliation
restores missing rejection rows without duplicating them. Recording is best
effort when the state filesystem itself is unavailable; the original refusal
is still returned. Dashboard reads never write into notes.

## Artifacts

An artifact is the second first-class kind beside the note: a static file or a
link that was handed in or produced, stored byte for byte. It is never a note,
and no route turns one into the other. Every submission declares `kind:
artifact`; a submission with no kind, or any other kind, is refused with the
reason. The bytes live immutably in the archive under the artifact identity
(`/data/artifacts/<id>/rNNNN/<filename>`), a browsable copy lands under
`Artifacts/{yyyy}/{yyyy-mm}/<filename>` in the notes filesystem keyed on the
received date, and the extracted text and provenance live in a record under
`/data/state/artifacts/<id>.json`, mirrored into PostgreSQL and the search
index. Nothing generated sits beside the file in Obsidian. A note cites an
artifact in its `artifacts` frontmatter list; filing a note never moves an
artifact, and deleting a note leaves the artifact uncited.

| Method and path | Scope | What it does | Success | Failure |
|---|---|---|---|---|
| `POST /v1/artifacts` | `artifacts:write` | Store a file or a link. `multipart/form-data` with a `file` part plus text fields, or JSON `{kind, url}` or `{kind, filename, content_base64}`. Fields: `kind` (required, `artifact`), `title` (defaults to the filename stem or the URL), `date` (received, `YYYY-MM-DD`, defaults to today), `tags`, `origin`, `note_id` (a note that cites it: the citation is written into its frontmatter and a link into its body), `artifact_id` (store the upload as a new revision of that artifact; earlier revisions stay), `mime_type`. Bounded at `limits.artifact_max_bytes` (10 MiB by default), read from the store, measured on the decoded file bytes (base64 and multipart framing do not count) and enforced while the body streams in, so an oversize upload is stopped before it is buffered or spooled, with or without `Content-Length`; a larger file is refused with 413 `payload_too_large` and one plain message, the same for multipart and JSON and whichever check caught it: "This file is larger than the 10 MiB limit. Files over 10 MiB can't be added yet; split or compress it, or add a link to it instead." (`limit_bytes` carries the number). Identical bytes under the same filename, or the same URL, replay | 201, `{id, kind, revision, created, path, title, note_id}`; 200 `created: false` on a replay | 422 `validation_error` (no or wrong kind, bad URL, a filename that is a path, an unknown `note_id` or `artifact_id`); 413 `payload_too_large`; 503 |
| `GET /v1/artifacts` | `artifacts:read` | List artifacts newest received first, filtered by `tag`, `origin`, citing `note_id`, and `from`, `to` on the received date; paged by `cursor` and `limit` | 200, page of summaries, each with `kind: artifact`, `title`, `origin`, `received_at`, `filename`, `mime_type`, `sha256`, `size_bytes`, `url`, `tags`, `path`, `revision`, `citing_note_ids`, `citing_notes` (each `{id, title, path}`, so a person or an agent can name the note), `text_status`, `state` | 422; 503 `metadata_unavailable` |
| `GET /v1/artifacts/{id}` | `artifacts:read` | The metadata record: everything the summary carries plus every revision and the full extracted `text`. This is the only way to read the record; it is never a page in the notes filesystem | 200 | 404 |
| `GET /v1/artifacts/{id}/bytes` | `artifacts:read` | The archived bytes of the current revision, or of `?revision=n`, with the recorded media type when it is a plain token, `Content-Disposition: attachment`, and `X-Coppermind-SHA256` | 200 | 404; 422 for a link, which has no bytes |
| `DELETE /v1/artifacts/{id}` | `artifacts:write` | Remove an artifact the way the Admin page does, for an agent acting for the owner: the browsable copy, the record, the mirror row and the search entry go at once; the archived bytes stay under the artifact identity, marked `removed through the API`, until the retention purge removes them after its window. Citing notes keep their citation | 204 | 404 for an unknown or already removed artifact; 403 without the scope |
| `PUT`, `PATCH` on `/v1/artifacts/**` | | Always refused: the archive is immutable; a changed file is a new revision through `POST` with `artifact_id` | | 405 `method_not_allowed` |

Obsidian and the API an agent uses are the everyday interfaces; Admin is for
oversight and repair. Deleting an artifact's file in Obsidian removes the
artifact: once the Git helper commits that deletion, the next reconciler pass
retires the record and the search entry, marks the archive `deleted in
Obsidian`, and the retention purge removes the bytes after its window, the same
as a removal from Admin or the API. A copy that is merely absent, because a
device has not synced yet or at a rebuild from disk, is reported `missing`
under Problems and nothing is lost: the archive stays until the copy comes back
or the deletion is committed. Moving or renaming the file in Obsidian is not a
delete: the reconciler finds the stored bytes at the new path, puts the file back at
its filed `Artifacts/{yyyy}/{yyyy-mm}/` location, logs why, and never retires the
artifact. A deletion only counts when it was committed after the artifact was
stored, and the Git lookup treats the path literally, so a name with `[`, `*` or `?`
cannot match another file's deletion. Editing the file in Obsidian never changes the
stored original; the edit is reported `modified`.

`text_status` says what extraction produced: `extracted`, `no_extractable_text`
(a readable type with nothing in it, such as an image-only PDF), `unsupported`
(no parser for the type; images, video and audio), `failed` (the parser failed
or exceeded its budget, listed under Problems) or `link`. PDF, PPTX, DOCX and
text types are extracted, in a subprocess with a memory and CPU budget; OCR is
out of scope. A record carries the extractor version and is regenerated by the
reconciler when that version changes. `state` is `ok`, or `modified` when the
browsable copy no longer hashes to the archived bytes, which Problems reports
and nothing rewrites.

Dropbox takes an artifact only with a kind marker: a sidecar named
`<filename>.kind` beside the dropped file containing the word `artifact` (or
JSON with `kind: artifact` and optional `title`, `tags`, `date`), or
`kind=artifact` on `POST /v1/dropbox`. Without one a non-calendar drop is
parked as unsupported with that instruction, never guessed at. A marked drop skips
Dropbox import history: the same file dropped again after its artifact was removed
is stored again, and identical bytes under the same filename replay as for the API.

## Search and status

| Method and path | Scope | What it does | Success | Failure |
|---|---|---|---|---|
| `GET /v1/search` | `notes:read`; artifact hits only with `artifacts:read` (without it they are left out and the search still succeeds) | Full-text search over reviewed, successfully filed note bodies and titles, their approved generated source pages under the sources folder, and the extracted text of every stored artifact (indexed when stored, with no review gate, because nothing about an artifact is a person's unreviewed draft). `q` (1 to 500 characters) is read the way a search box is: words, `"quoted phrases"`, `or`, and `-word` to exclude. English stemming applies, so `runs` also finds `running`, and an excluded word is excluded in every form: `budget -runs` finds no note that says `running`. Takes every listing filter (`folder, reviewed, type, context, account, from, to, tag, state`) with listing's meaning (`account` matches any entry of a note's list, `from` and `to` take the day from `date`; a frontmatter filter matches notes only, while `tag` matches notes and artifacts), plus `kind` (`note`, `source` or `artifact`) and `citing_note` (only artifacts that note cites), and pages the same way, by `cursor` and `limit` | 200, page of `{id, kind, title, path, folder, excerpt, rank, state}`, best match first. `kind` is `note`, `source` for a generated source page (whose `id` is the source identifier), or `artifact` (whose `id` is the artifact identifier and whose `excerpt` comes from the extracted text, so an agent can use the hit without fetching the file). `excerpt` is a short passage with every matched word between `**` markers. `rank` orders one answer and means nothing across answers | 422 `validation_error` for an empty `q`, a bad filter, or a cursor from a different `q`; 503 `metadata_unavailable`, never an empty page |
| `GET /v1/notices` | `notes:read`; a notice about an artifact only with `artifacts:read` (without it, those are left out) | Current notices, newest first: what Coppermind moved back after a move or rename by hand in the vault. Each is `{note_id, title, from_path, path, reason, at, kind}`: `kind` is `note`, `folder` or `artifact`, `note_id` is the note's or the artifact's identifier, `from_path` is where the file was found and `path` where it went back. An artifact's reason says it is filed by the month it arrived, a move or rename in Obsidian is not kept, and nothing needs doing. The same list Admin's overview and Artifacts page show. A notice about a note or artifact that no longer exists is dropped | 200, list | 503 `metadata_unavailable` |
| `POST /v1/notices/{note_id}/dismiss` | `notes:write`; an artifact's notice also needs `artifacts:write` | Hide a notice. Nothing moves: the file stays where Coppermind put it. Dismissing a notice that is already gone succeeds | 204 | 403 `forbidden`; 503 |
| `GET /v1/status` | any key | Version, capabilities, note and job counters, and per-helper `{last_success_at, age_s, ok}`. The counters are `{notes_awaiting_review, notes_by_state, sources, artifacts, artifacts_returned, rejected_ingests, name_collisions, unparseable_files, excluded_count, notes_folder, notes_to_refile, notes_returned, folders_kept, filing_failures, filing_queue_depth, filing_ready_depth, filing_estimated_drain_s, filing_last_progress_at, filing_stalled, calendar_import_failures, known_unreadable, purge_problems, active_problems}`, computed from the metadata mirror, recorded rejections and import receipts; `active_problems` counts every Problems row except intentional exclusions and returned-note notices, including calendar, Dropbox, retention purge and artifact problems; `purge_problems` counts the retention purge ones; `notes_awaiting_review` counts notes directly in the configured `notes.review_folder` with `reviewed: false`; `notes_folder` is the configured reviewed-notes root, `notes_to_refile` counts reviewed notes still awaiting the move into its `{yyyy}/{yyyy-mm}/` layout, and `notes_returned` and `folders_kept` count the current `returned_note` and `folder_kept` notices | 200 | 503 `metadata_unavailable` or `store_unavailable` |
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
| Overview | `GET /v1/admin/status`, `POST /v1/admin/notices/{id}/dismiss` | Counters, helper status and ages, last reconcile, last commit, last sync, failed jobs, and a filing summary (Filing in progress, Filing needs attention or Filing complete, with counts and last progress). Lists every artifact moved or renamed by hand in Obsidian and returned to its folder: the title, where it was found, where it went back, when and why, with a Dismiss that only hides the notice |
| Problems | `GET /v1/admin/notes/problems` | Every refused ingest, name collision (several files carrying one note identity) and unparseable file, each linked to the note or source it is about. Also lists failed calendar imports, unsupported or failed Dropbox receipts and calendar conflicts, unfinished or abandoned retention purges, unreadable purge receipts and damaged source bundles, artifacts whose browsable copy changed on a device or whose text could not be extracted (`artifact`), and per-note filing errors (`filing`), missing notes (`missing`), path disagreements (`path_notice`), unreadable notes (`unreadable_note`), hand-moved reviewed notes returned to their filed folder (`returned_note`) and old month folders left because they still hold files (`folder_kept`). The Admin page opens with the same filing summary as Overview (without Overview's link back to this page), built from `GET /v1/status`. Each row has a snake_case `kind` slug; the calendar, Dropbox and artifact rows use `calendar_import` (a failed import receipt), `dropbox` (a failed or unsupported Dropbox file), `calendar_conflict` (two sources disagree on one event at the same revision) and `artifact`. Computed from the mirror, recorded rejections and import receipts; nothing is ever written into a note |
| Artifacts (`/admin/artifacts`) | `POST /v1/admin/artifacts/{id}/remove`, `POST /v1/admin/notices/{id}/dismiss` | Every stored artifact: title (with, when it did not arrive through the API, where it came from), received date, size, path in Obsidian or link, the citing notes by title, text status and revision, laid out to read at 1280 pixels wide without a date, path or button wrapping. Artifacts moved by hand and returned to their folder are listed above the table with the same notices the overview shows. Remove, after a confirmation page that shows the identifier and says what happens to a file (the copy in Obsidian is deleted, search forgets it, the stored file is kept until the retention purge) or to a link (nothing in Obsidian changes; it is no longer listed or searchable; notes keep the address), takes the browsable copy, the record and the search entry away; the archived bytes stay under the artifact identity until the retention purge removes them (see [Retention purge](#retention-purge-implemented)) |
| API keys | `GET/POST /v1/admin/keys`, `DELETE /v1/admin/keys/{key_id}` | Create (secret shown once), list, and revoke keys graphically instead of through the store's command line |
| Obsidian Sync | `GET /v1/admin/sync`, `POST /v1/admin/sync/connect`, `POST /v1/admin/sync/token`, `POST /v1/admin/sync/pause`, `/resume`, `/disconnect` | Connect with email, password, optional MFA, and a vault name or a pasted token; reveal a token once, with a hint for sealing it into GitOps; pause and resume around bulk operations |
| Settings | `GET/PUT /v1/admin/settings {if_revision, body}` | Every key in the settings table below, grouped by section |
| Frontmatter schema | `GET/PUT /v1/admin/schema`, `POST /v1/admin/schema/rename-key {from, to}` | Keys, kinds, and vocabularies; renaming a key runs the migration job that rewrites every note through the store |
| Fields and tags (`/admin/fields`) | `POST /v1/admin/fields {revision, ...}` | Add, edit and retire keys (kind, required and required when, allowed values each with a one-line meaning, default, guidance); every tag in use with its note count from the mirror, a meaning and aliases per listed tag, and open or closed tags. Saves with the same revision check Settings uses; the role map is carried over untouched, and nothing here rewrites a note |
| Filing rules | `GET/PUT /v1/admin/rules`, `POST /v1/admin/rules/preview {note_id}` | The ordered rules a note is filed by, with a dry run ("what would this note file as") before committing to a change |
| Search index (`/admin/search-index`) | `POST /v1/admin/search-index/rebuild` | The number of notes, source pages and artifacts indexed, the known unreadable count, when the index last changed and when it was last rebuilt, in the operator's timezone, and a Rebuild index button. A rebuild runs in the store's background, throws every entry away, except the last indexed text of a note that is currently unparsed, and reads the notes filesystem again; search keeps answering from the old entries until the new ones commit |
| Retention purge (`/admin/purge`) | `POST /v1/admin/purge/preview`, `/confirm`, `/abandon`, `/settings` | Edit the retention window, preview what a manual purge would remove, confirm it, retry or abandon an unfinished one. See [Retention purge](#retention-purge-implemented) |
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
- **Artifacts:** store one (file or link, declared kind), get its record, open
  its bytes for a revision, list with filters, read the inline upload limit,
  remove its projection (`DELETE /internal/v1/artifacts/{id}?removal=admin|api`;
  the archived bytes stay, marked with the reason).
- **Notices:** list what the reconciler moved back after a move or rename by
  hand (`GET /internal/v1/reconciler_notices`) and dismiss one
  (`POST /internal/v1/reconciler_notices/{id}/dismiss`).
- **Schema and tags:** read the frontmatter schema the store enforces, with
  its file revision (`GET /internal/v1/schema`), and count notes per tag in
  the mirror, a note counting once per tag (`GET /internal/v1/tags`).
- **Control state:** get and put a named state file (`settings`, `schema`,
  `rules`, `keys`, `admin`), each guarded by the revision the caller last
  read.
- **Search:** full-text search with listing's filters (`GET
  /internal/v1/search`), the index's counts and times (`GET
  /internal/v1/search/index`), and starting a rebuild of the index from the
  notes filesystem (`POST /internal/v1/jobs/rebuild_search_index`, 202 with
  `started: false` when one is already running).
- **Retention purge:** preview, confirm and abandon a manual purge
  (`POST /internal/v1/purge/preview`, `/confirm`, `/abandon`).
- **Jobs and status:** start a job by kind, check a job's progress, read
  overall store status, list the problems Admin shows.

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
  as `unparsed` with a reason. Search keeps finding it by the text it was
  last indexed with, at the path it is now found at, and reports it with
  `state: "unparsed"` until the file parses again.
- **A move never breaks a link; a rename can.** Obsidian resolves
  `[[wikilinks]]` by filename, so the curator only ever moves a note
  between folders and never renames one. The rename endpoint exists for a
  person to use deliberately, and it documents that link rewriting is not
  part of it.

Search catch-up remembers a failed read with its reason, content hash or source
revision, path, and filesystem observation, but only when the failure is
durable: a missing file or projection, or content that cannot be parsed. A
transient filesystem error (EIO, EACCES, EMFILE, ESTALE and the like) is
logged and left unmarked, so the next pass retries it. Unchanged marked
failures do not consume future catch-up batches. A revision, path, manifest, or
projection file change makes an entry eligible for another read; Rebuild index
also clears the marks and retries. Each new failed observation logs `search
entry unreadable` with its identity, path, and reason. Catch-up summaries
distinguish `unreadable` (failures this pass) from `skipped_known_unreadable`,
and the `search index caught up` line is logged only when a pass indexed,
removed or failed something. Both search index status and store status expose
`known_unreadable`; Admin Problems lists the entries and their reasons. These
marks do not remove source archives or notes.

## Calendar exports and Dropbox receipts

Coppermind has one calendar. Export sources are provenance on each event, not
separate calendars. Calendar UID binding and identity claiming are removed.

| Route | Scope | Result |
|---|---|---|
| `POST /v1/dropbox?filename=export.zip` | `sources:write`; `kind=artifact` also needs `artifacts:write` | Binary ICS or ZIP, 202 after durable intake; 413 above 200,000,000 bytes, or above `limits.artifact_max_bytes` when `kind=artifact` |
| `GET /v1/dropbox` | `sources:read` | `{folder, items}`; each receipt has `id`, `filename`, `state`, `error`, `event_count`, `first_imported_at`, and import references in `parts`; files still being copied have state `stabilizing` and an empty `id` until accepted |
| `POST /v1/dropbox/{id}/dismiss` | `sources:write` | Dismiss a completed, failed or unsupported receipt; 422 for pending/importing or unknown receipts |
| `GET /v1/calendars?offset=0` | `sources:read` | Compatible `{calendar, events, contacts, offset, next_offset, totals}` response; calendar now also has `id: calendar`, `name: My calendar` |
| `POST /v1/calendars/calendar/imports/{id}/dismiss` | `sources:write` | Dismiss a failed import receipt, whether it arrived by Dropbox, direct upload or the upgrade migration; the receipt stays in import history marked dismissed and leaves Problems; 422 for unknown or non-failed receipts |
| `POST /v1/calendars/calendar/imports?filename=export.ics` | `sources:write` | Compatibility binary upload, 202 durable import receipt; other calendar IDs return 422 |

Dropbox states are `pending`, `importing`, `succeeded`, `already_imported`,
`failed`, and `unsupported`. Completed receipts expire after seven days; failed receipts remain until dismissed.
Dismissal never deletes original exports or calendar import history. An identical
re-drop is `already_imported` and does not create another import. A dismissed
unsupported file stays dismissed while it sits unchanged in the folder; changing
the file or adding a `.kind` sidecar to it brings it back.

Events retain their existing fields and add `provenance`, a list of
`{source_id, filename, entry, imported_at, legacy_calendar}` witnesses. The last
field records a previous calendar on upgrade, otherwise an empty string.
Timestamps use ISO 8601 with an offset. Multiple witnesses are expected, even
when the event revision is unchanged or older. Pagination remains 100 records per
section, following `next_offset`. No source filter is supported. Expanded ZIP
limits remain 40 MiB and 100 entries; conflicts fail the export atomically and
appear in Admin Problems. Without authentication these routes return 401;
without the named scope they return 403.

## Retention purge (implemented)

The public API service accepts API keys explicitly holding `admin:purge`:

| Method and path | Request | Response |
|---|---|---|
| `POST /v1/admin/purge/preview` | Empty body | Saved dry run: `token`, `state`, `created_at`, `retention_days`, `candidates`, `counts`, `kept`, `completed`, `remaining`, `error` |
| `POST /v1/admin/purge/confirm` | `{"token":"<preview token>","confirmation":"Purge listed items"}` | Same candidate list and counts, `state: complete` on success |
| `POST /v1/admin/purge/abandon` | `{"token":"<token of an unfinished or unreadable receipt>"}` | The receipt with `state: abandoned`; `completed` counts listed items now gone and `remaining` lists the ids that are not |

Candidates include `kind` (note, source or artifact), `id`, `title`, `path`, `origins`,
`providers`, `deleted_at` for notes and for artifacts (when they were removed), and `reason` for artifacts (`removed in Admin`, `removed through the API` or `deleted in Obsidian`). Counts
contain `notes`, `sources`, `artifacts`, `by_origin`, and `by_provider`; each origin and
provider carries its own `notes`, `sources` and `artifacts` counts, so a deleted note is never
mistaken for a removed source. `kept` reports what the dry run left alone:
`notes_within_retention`, `sources_in_use` and `artifacts_within_retention`. This dedicated scope is never added to an
existing key; `admin:write`, `notes:delete`, and source scopes do not authorize
purge. A changed candidate set, setting, mirror row or file yields 409
`purge_conflict`; malformed or absent confirmation yields 422. Storage or
metadata failures yield 503 and retain the receipt for inspection and retry.
An unfinished purge is returned by preview with `state: applying` or `failed`;
confirming its same token retries its saved list, after checking live references
and surviving file hashes again. A completed token returns its prior result.
Abandon stops an unfinished (`applying` or `failed`) list without retrying it
and releases its ids so reconciliation and a fresh preview proceed. It reports
what was and was not removed, and the items still present appear in Problems.
It also moves an unreadable receipt file into quarantine. Confirming an
abandoned token is refused with 422; run a new preview. Preview, confirm and abandon
can run long on a large vault, so the store client allows them 10 minutes.

Internal equivalents are `POST /internal/v1/purge/preview`, `/confirm` and `/abandon`,
protected by the internal token. Admin's `/admin/purge` page uses the same store
contract through its session and CSRF guarded forms at `/v1/admin/purge/preview`
`/confirm` and `/abandon` on the separate Admin service. `/v1/admin/purge/settings` on
Admin saves `days` and `revision` from its form; the setting takes effect on the
next preview. The two services keep their existing authentication boundaries.

Purge is a separate removal operation. PUT, PATCH and DELETE on
`/v1/sources/**` still always return 405. No live-reference override exists.
The precise retention and failure recovery rules are in
[data and settings](data-and-settings.md#retention-purge-receipts).
