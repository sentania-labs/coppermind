# Coppermind data, schema, and settings (approved plan)

Every product setting, the frontmatter schema, the filing rules shape, and
the storage formats, as approved. This is the reference someone would need
to operate or rebuild the product-data side of Coppermind without the
original planning record. Live status is [STATUS.md](../STATUS.md); the
component and process picture is [architecture.md](architecture.md).

The organizing rule underneath everything here: every one of these is a
plain, versioned file under `/data/state`, not a database row. PostgreSQL
holds a mirror of each one purely for fast querying; the file is the truth,
and losing PostgreSQL loses nothing, because one job rebuilds every mirror
from these files and from the notes themselves.

## The note file itself

```markdown
---
schema_version: 1
id: 01K4Q8Z3N7V2X9M1B5C6D8E0F2
type: work
context: external
account:
  - Acme
reviewed: false
sources:
  - 01K4Q8Z2A0P1Q2R3S4T5U6V7W8
tags: []
date: 2026-09-08T14:02:11
---
# Acme Architecture Sync
...
```

| Rule | Value |
|---|---|
| Required keys | `schema_version` (int), `id` (ULID), `type`, `context`, `reviewed` (bool), `sources` (list of ULID) |
| Kind | `kind` is a declared enum, `note` or `artifact`, defaulting to `note` and written on every note Coppermind creates. A note is always `note`: a file declaring `kind: artifact` is refused on create, replace, patch and adoption, and an artifact is never a note. `artifacts` (list of ULID, optional, written only when present) names the artifacts the note cites |
| Optional keys | `date` (`YYYY-MM-DD`, or a local date-and-time `YYYY-MM-DDTHH:MM:SS` with no UTC offset in the configured timezone, `general.timezone`, `America/Chicago` by default; the one field for a note's date and start time, written as a date-and-time on new notes; a UTC offset is rejected at review validation), `time_unknown` (bool; a work note with a date-only `date` needs it), `account` (a list of customer names; a plain string is read as a one-item list; at least one entry is required when `context: external`), `tags` (list), `title` (only when it differs from the heading), `external_refs` (a passthrough map), `unresolved` (set by the curator on an ambiguous filing), `managed` (projections only). A legacy `HH:MM` `time` beside a `date` still validates and files, and a legacy `timezone` is ignored; when a legacy `time` carries its own date and disagrees with `date`, `date` wins and Problems records a `date_mismatch` entry |
| Shipped `type` suggestions | `work`, `personal`, `reference`, `journal` (default: `reference`); `type` is a free string, so any other value is accepted and files under a folder of that name |
| Shipped listed tag | `reference`: reference material of either kind, a how-to you wrote or an upgrade deck; it changes nothing about storage or filing |
| Shipped `context` vocabulary | `internal`, `external`, `personal` |
| Filenames | Dated types: `YYYY-MM-DD Title.md`. Undated: `Title.md`. Journal: `Journal/YYYY/YYYY-MM-DD.md`. Names are Unicode-normalized, Windows-reserved characters and device names are stripped, capped at 120 characters, and a case-insensitive collision gets a numbered suffix |
| Identity (`ETag`) | `"sha256:<hex of the file's bytes>"`. A move does not change it; a frontmatter write-back does |
| Unparseable file | Left byte-for-byte untouched, recorded as `state: unparsed` with a reason, indexed by body if it can be decoded at all |
| Duplicate ID | The path the mirror last recorded keeps the ID, and no file is rewritten: both are left as they are and reported under Problems with every path. A copy left in the review folder after its note was filed is removed when its bytes equal the filed note's (or the hash of a version filing recorded of it, before or after filing rewrote its links; the filing receipt `state/filing/<id>.json` keeps only those hashes, never content, the newest 16 per note, for as long as the note exists, so a copy that turns up days later is still recognised); one that differs is kept, named beside the filed path on `GET /v1/notes/{id}/review` as `review_problem` and on Problems. Each unresolved collision is logged when first seen and when its paths change |
| ID assignment for a device-created file | The reconciler writes the ID, and any other frontmatter the schema requires, once the file has gone 30 seconds (configurable) without an mtime change, using a compare-and-swap on the content hash so a still-in-progress edit is never interrupted |

**Field guidance and tags** live in the same `schema.yaml`. Each key may
carry `vocabulary_meanings` (a one-line meaning per allowed value) and a
`guidance` paragraph for whoever fills it in; neither changes validation.
Three top-level entries make tags data: `tags` (each listed tag and its
meaning), `tag_aliases` (an alias and the canonical tag it is replaced by on
every write), and `tag_mode`, `open` by default (any tag accepted and counted)
or `closed` (a write that adds an unlisted tag is refused). Admin's Settings,
Properties and Tags pages (`/admin/settings/properties`, `/admin/settings/tags`)
edit it under the file's revision. Merging or renaming a tag across existing
notes is a later job, not part of these pages.

**Key roles**, not literal names, are what code asks for: `id_key`,
`date_key`, `type_key`, `context_key`, `account_key`, `reviewed_key`,
`sources_key`, `tags_key`. Renaming a frontmatter key (`account` to
`customer`, say) is a change to this role map plus a migration job that
rewrites every note through the store with a minimal diff and one Git
snapshot, never a code change.

## Source bundles and their generated pages

`manifest.json` beside each source, schema version 1:

| Field | Notes |
|---|---|
| `source_id` | ULID |
| `provider`, `external_source_id` | Identity of a source is the pair `(provider, external_source_id)`; a repeated pair replays instead of duplicating |
| `source_type` | `transcript`, `document`, `image`, `email`, `other` |
| `origin` | Free text: a device or mailbox name |
| `current_revision`, `revisions[]` | Each revision records when it was ingested, when it was captured, a content identity hash over its artifacts, and passthrough metadata |
| `revisions[].artifacts[]` | Each artifact's name, MIME type, SHA-256, and size |
| `tombstoned_at`, `tombstone_reason` | Reserved for the deferred tombstone action; not exercised yet |

Artifact files live under `r<NNNN>/` inside the source's own directory,
created exclusively and never opened for writing again once they exist.
Content identity is the hash over the sorted artifact hashes, not the raw
bytes of the request, so a client that re-normalizes text on retry does not
manufacture a false new revision.

The generated projection page, one per source, always showing the latest
revision: `_Sources/<Provider>/<YYYY-MM-DD Title>.md`, marked `managed:
true` in its own frontmatter, carrying the source's identity and revision.
It is regenerated whenever the revision changes, is excluded from Git
history (it is derivative, not a person's own writing), and is still synced
to devices, which is the entire point of it existing.

## Artifacts: archive, browsable copy and record

An artifact has three homes, each with one job:

| Where | What | Durable? |
|---|---|---|
| `/data/artifacts/<id>/manifest.json` and `rNNNN/<filename>` | The immutable bytes of every revision, created exclusively and never rewritten, with the manifest naming kind, title, origin, received date, tags, URL, browsable path, source link when filing made it, and every revision's filename, media type, SHA-256 and size. A removed artifact keeps its archive, marked `removed_at` with `removed_reason` (`removed in Admin`, `removed through the API` or `deleted in Obsidian`, each eligible for the retention purge after its window) | The truth, in the backup set |
| `<artifacts folder>/{yyyy}/{yyyy-mm}/<filename>` under `/data/notes` | The browsable copy Obsidian shows, by original filename, keyed on the received date. It follows the current revision at a stable path so links keep working; a copy a device changed is never rewritten and is reported under Problems. Deleting it in Obsidian removes the artifact once Git records the deletion (`deleted in Obsidian`, purge-eligible); a deletion committed before the current revision was stored never counts. Moving or renaming it is not a delete: a file anywhere under the vault holding the archived bytes is returned to this filed place (the Dropbox folder, dotted folders, links, and copies that were already in the vault before the current revision was stored are never taken, so an original dropped for ingest or a duplicate you keep is not consumed and cannot undo a delete), the artifact stays, and a notice is recorded in `/data/state/reconciler_notices.json` (the same list a hand-moved reviewed note uses) saying what moved, where it was found, where it went back and why; Admin's Home shows it, `GET /v1/notices` returns it, and dismissing it only hides it. A copy merely absent, or a vault search that could not finish, is reported `missing` and the archive stays. A link artifact has none. Nothing generated sits beside it | Synced and in Git like any file |
| `/data/state/artifacts/<id>.json` | The disposable record: provenance plus the text the extractor read out of the current revision, and the extractor version. Mirrored into the `artifacts` table and the search index; regenerated by the reconciler when the extractor changes; removed when the artifact is removed or its browsable copy is deleted on a device. Never a page in the notes filesystem | Rebuilt from the archive by `rebuild_metadata` |

Recording audio submitted for transcription is an exception: after a nonempty transcript
is reviewed and filed, its audio bytes are removed, while the manifest retains its hash
and size. The note records the duration and optional `metadata.archive_url` location.
This does not wait for the retention purge: if the owner already removed the recording
artifact, its archived audio is still deleted once the note is reviewed and filed, and the
purge never has to remove it.

Legacy `_Attachments/YYYY-MM/<source>-rNNNN-<name>` files are moved into the
artifacts folder by the reconciler, registered as artifacts of their source
with the source revision's ingest date, and every note or source page that
linked the old path is repointed. Filing a reviewed note's source material
lands its non-text, non-audio artifacts here the same way, and the note gains
the citation.

## Recorded ingest rejections

Each refused ingest recorded by the store has an immutable
`/data/state/rejections/<record_id>.json` file. Schema version 1 holds
`record_id` (ULID), `kind: ingest`, `reference` (provider and external ID),
`reason` (error code), and `created_at` (timestamp with a UTC offset).
It contains no submitted artifact or note content. Reconciliation restores
the `recorded_rejections` mirror by record ID, retaining separate attempts
without duplicating them on later scans. Name collisions are computed anew
from observed note files on each scan.

## Settings (`/data/state/settings.yaml`)

Every key below has a working default; the ones a person changes have a place
in Admin. Nothing here needs to be set before the system runs.

| Section | Key | Default |
|---|---|---|
| general | `timezone` | `America/Chicago` |
| notes | `notes_folder` | `Notes` |
| notes | `review_folder` | `Review` |
| notes | `trash_folder` | `_Trash` |
| notes | `sources_folder` | `_Sources` |
| notes | `artifacts_folder` | `Artifacts` (holds `{yyyy}/{yyyy-mm}/<filename>`) |
| notes | `attachments_folder` | `_Attachments` (legacy; migrated into the artifacts folder) |
| notes | `journal_folder` | `Journal` |
| notes | `dated_types` | `[work, journal]` |
| reconcile | `scan_interval_s` | 60 |
| reconcile | `quiet_period_s` | 30 |
| reconcile | `full_rehash_daily_at` | `03:30` |
| git | `enabled` | true |
| git | `debounce_s` | 60 |
| git | `poll_interval_s` | 300 |
| git | `identity_name` | `Coppermind` |
| git | `identity_email` | `coppermind@localhost` |
| git | `gc_auto` | true |
| sync | `plan` | `standard` (operator uses Plus; see roadmap.md) |
| sync | `max_file_bytes`, `max_total_bytes` | Null uses the plan. Plus: 200 MiB and 10 GiB (binary; approximately 200 MB and 10 GB). Set explicitly to override. |
| sync | `device_name` | `coppermind-server` |
| sync | `mode` | `bidirectional` |
| sync | `conflict_strategy` | `merge` |
| sync | `excluded_folders` | `[_Trash]` |
| sync | `file_types` | `[image, audio, video, pdf, unsupported]` |
| sync | `sync_configs` | `[]` |
| curator | `enabled` | true |
| curator | `sweep_interval_s` | 300 |
| curator | `inbox_only` | true |
| indexer | `enabled` | true |
| indexer | `language` | `english` |
| indexer | `reconcile_interval_s` | 600 |
| retention | `days` | 30 (integer, 0 through 36500; 0 permits immediate manual purge). The one window for deleted notes and for removed artifacts, whether removed in Admin, through the API or by deleting the file in Obsidian |
| events | `retention_days` | 7 |
| events | `poll_fallback_s` | 5 |
| limits | `calendar_max_events` | 50,000 retained events across calendars |
| limits | `calendar_import_memory_mib` | 1024 MiB parser address space; about 40x the expanded export size |
| limits | `ingest_max_bytes` | 25 MiB |
| limits | `artifact_max_bytes` | 10 MiB (10,485,760 bytes) per inline artifact file; a larger file is refused with the limit and what to do now (use the staged upload path, or split or compress it) |
| limits | `staged_max_bytes` | 1 GiB (1,073,741,824 bytes) per staged upload; the ceiling a `POST /v1/staged` declaration is measured against. Uploading, finalizing, serving the bytes back, and verifying or hashing the archive all work a megabyte at a time, so the limit costs disk, not memory |
| limits | `attachment_max_bytes` | null (legacy; superseded by `artifact_max_bytes`) |
| admin | `session_hours` | 12 |

Every control state file, including this one, carries `schema_version` and
`revision`. A write must state the revision it is replacing, and the file it
replaces moves to `/data/state/history/`, so nothing is ever silently lost
to a concurrent edit.

### Transcription service

A recording handed in on its own is transcribed by a service chosen on **Admin >
Transcription**, from the AI services entered under **Admin > AI services**. When a
service is added, edited or tested, Coppermind sends it a made-up 10-second recording;
only a service that wrote it up (any OpenAI-compatible one that offers `POST
/audio/transcriptions`; Anthropic does not) can be chosen. The assignment holds the
connection, the model and the reply format (`diarized_json` or `verbose_json`) that
test found, so nobody types either, the wait limit (default 3,600 seconds, chosen from
1,800, 3,600, 7,200 or 14,400: how long the service may take to answer one recording,
since most send nothing until they have finished), the total attempts per recording
(default 2) and the on and pause switches. It is off until switched on, and the
recording is sent only to that service. A recording handed in while transcription is
off is refused with a plain message and nothing is queued to run later. A recording added
from Admin while it is off is kept as a file, listed as not written up, and written up
later only if its owner presses "Write it up now" on its line. The services and what each test found
are kept in `/data/state/context/ai-services.json`. Saved in `/data/state/context/config.json` as `assignments.transcription`;
each intake is one small file under `/data/state/context/recordings/` and its saved
transcript under `/data/state/context/recording-transcripts/`. Voice signatures are
private state in the same folder (mode 0600, never in the vault or an API response):
`voice-signatures.json` (global recognition setting, saved signatures, recording
assignments, turned-down suggestions),
`voice-pending/<recording id>.json` (a recording's signatures, removed when its
review completes), `voice-mappings/<recording id>.json` (the unknown-voice names given
to that recording's speakers) and `voice-settings.json` (the two match thresholds). Rules in
[dossiers](dossiers.md#voice-recognition).

### Meeting summary directions

The directions for each section of a recording's meeting summary are saved as
`summary_directions` on the enrichment assignment in
`/data/state/context/config.json`: a map of section key (`summary`,
`decisions`, `action_items`) to text of at most 1,500 characters. A missing or
empty entry means that section's default. They are edited under **Admin >
Suggestions > How the meeting summary is written**, apply from the next
summary, and never rewrite one already made. Rules in
[calendar and enrichment](calendar-enrichment.md).

### Calendar import budgets

**Admin > Calendar** sets `limits.calendar_max_events`;
`limits.calendar_import_memory_mib` is set in the settings file. Both take effect
on the next import.
The first bounds retained event identities, including cancellations, in the one
calendar. Updating an existing identity does not consume another slot. Lowering
it does not delete already accepted events or prevent serving them.

The memory setting bounds each calendar parser subprocess's address space.
A preflight estimate of about 40 bytes per
expanded byte plus 5.3 KiB per event rejects exports that would exceed it before
constructing the parser's object graph. The subprocess also has a hard address-space limit and a 45-second deadline. These are import limits,
not a container RSS limit; reserve memory for the store and requests as described
in [calendar sizing](calendar-enrichment.md#memory-and-deployment-sizing).

An over-budget import retains its original, marks its receipt failed with the
setting name and ceiling, and appears in **Problems**. The previous calendar
remains queryable. Raise the budget only after sizing the deployment, or submit a
smaller partial export, then drop the corrected export. A budget refusal is a
deterministic failure: its failed receipt stays until dismissed in Admin. Dropping
the same export again re-runs it, so raise the budget first. Restarting does not
automatically retry failed imports.

## Filing rules (`/data/state/rules.yaml`)

An ordered list of predicates on frontmatter, each mapping to a path
template with placeholders for `{account}` (the first entry of the note's `account` list), `{yyyy}`, and `{type}`, plus a
fallback for anything that matches nothing: leave it in place and mark it
`unresolved: true` rather than guess. Shipped defaults:

| When | Files to |
|---|---|
| `context: internal` | `Work/Internal/` |
| `context: external` | `Work/External/` |
| `context: personal` | `Personal/` |
| `type: journal` | `Journal/{yyyy}/` |
| `type: reference` | `Reference/` |

## Keys and Admin state

`keys.json`: one entry per API key, `key_id`, `name`, an argon2 hash of the
secret, its scopes, when it was created, and when (if ever) it was revoked.
The API reads it again once its copy is more than two seconds old, so a removed
key stops working within about two seconds and a new key works within about two seconds.
`admin.json`: the argon2 hash of the Admin password, when setup was done, and
the session signing secret. Both are files under `/data/state`, mode 0600,
never database rows, because the API-key cache and the admin session both need
to keep working even if PostgreSQL is unavailable. The one-time setup code
waits in `/data/state/internal/claim-code` until a password is chosen with it,
and a new setup link's code in `/data/state/internal/reset-code`, where it
works for one hour.
`/data/state/context/pairing.json` holds the current companion pairing code
until it is used, runs out after ten minutes, or is spent by wrong guesses.

## Reviewed-note filing layout

Reviewed notes are filed under `{type}/{yyyy}/{yyyy-mm}/` by note date, using
the schema's type role. Folder names are sanitized and use the spelling of an
existing case-insensitive match. When several existing folders differ only by
case, the all-lowercase one is used, otherwise the first in plain codepoint
order. For new case variants reviewed in the same pass, the note with the
lowest ID creates the folder and chooses its spelling. Missing or empty types and non-string values
without a string list entry use `notes.notes_folder` as a fallback. Reserved
folders, including the reconciler's excluded folders, also use the fallback
and appear in Problems with a next step. A type or date edit refiles the note
on the next pass. Existing notes in the old notes root move on that pass and
stay searchable until then. Filed source pages go under
`{notes.sources_folder}/{yyyy}/{yyyy-mm}/`; pending source pages wait under `{sources_folder}/_Pending/{yyyy}/{yyyy-mm}/`.
`notes_folder` must be a portable relative folder that does not collide with
another reserved root. Artifacts are filed under the artifacts folder by the
same `{yyyy}/{yyyy-mm}` shape (see above); the retired `_Attachments/` folder is
migrated into it. Notes and source
pages filed in the old flat `YYYY-MM/` layout are moved on the next filing pass
through the same recoverable journal; an emptied old `YYYY-MM/` folder (at the
root or under `_Sources`) is removed, and one that still holds files is left
alone. A reviewed note moved by hand out of its filed folder is moved back on
the next pass.

`/data/state/reconciler_notices.json` records these returns and kept folders.
Problems lists each current notice, beside the path notices, with the reason
(`returned_note`, `folder_kept`); the operator can dismiss a returned-note
notice. The list holds only what is current: a notice is dropped when its note
is gone or has moved on from the returned path, and a kept-folder notice clears
itself when the folder empties or goes away. Home lists the returned notes under Recent activity.

## The PostgreSQL mirror

Nothing here is durable on its own; the `rebuild_metadata` job recreates all
of it from `/data`. Tables exist for: note metadata (path, hashes,
frontmatter fields worth querying by, observed state), source and revision
and artifact records, which note cites which source, the event outbox each
consumer reads from and acknowledges, each consumer's own read cursor, the
generated search index (full-text entries and, when an embeddings model is set up,
pgvector passages), a mirror of API key hashes, per-key last-used
timestamps, a mirror of every control-state file's current revision, job
history, per-component status (as reported by the helpers' own status
files), and admin sessions. None of it is the durable copy of anything; all
of it is a cache that makes listing and search fast.

### Search by meaning

Optional and off until an operator turns it on under Admin's Settings, Search: an
embeddings assignment names an AI service whose test showed it can make
embeddings (OpenAI or OpenAI-compatible) and the model that test found, stored
with the other model assignments in `/data/state/context/config.json` under
`embeddings`. The credential stays in the service's connection.

A background worker in the store gives every search entry (notes, generated
source pages, stored artifacts, and the people and company pages, whose text
includes personal details), newest first,
passages of about 1,200 characters with the title, asks the connection's
`/embeddings` endpoint for their vectors a few at a time, and stores them in
`search_embeddings` (pgvector, no fixed dimension, exact cosine distance).
Indexing and filing never wait on it. Vectors belong to the entry's version and
the model: an edited note, or a changed model, is embedded again, and a deleted
or purged entry takes its vectors with it (the table cascades from
`search_documents`). A timeout halves the batch and a failing service is retried
with a doubling wait up to 15 minutes; a text the service refuses repeatedly is
left out until it changes. Existing content is the same loop, so the backfill
needs no button and resumes after a restart. Dropping the table loses nothing.

The compose stack runs the `pgvector/pgvector` image in place of plain
`postgres`, same major version and data directory. On a PostgreSQL without the
extension the migration skips the table and search stays full-text. The store
creates the extension and the table itself when it next needs the table
(asking the database again at most once a minute), as soon as the server offers pgvector, so
installing it later, or moving to the pgvector image, needs no migration. If the
database user may not create extensions, Admin says so and gives the one
`CREATE EXTENSION` command a superuser runs.

## PostgreSQL image upgrade

The bundled PostgreSQL image uses pgvector 0.8.7 on Debian trixie, matching
PostgreSQL 16's collation library in the v0.6.4 compose image. Back up the
PostgreSQL data and the notes filesystem before upgrading. From v0.6.4, start
the updated compose stack normally; its migration job upgrades the database.

If the data directory has already run with the bookworm-based image from
v0.7.0 or v0.8.0, text indexes may have been built using a different sort
order. Stop the application and all other database clients, start only the
updated PostgreSQL service, then rebuild indexes before refreshing the recorded
collation version. Run these commands from the compose directory:

```bash
docker compose stop
docker compose up -d --wait postgres
docker compose exec postgres psql -v ON_ERROR_STOP=1 -U coppermind -d coppermind -c 'REINDEX DATABASE coppermind;' \
  && docker compose exec postgres psql -v ON_ERROR_STOP=1 -U coppermind -d coppermind -c 'ALTER DATABASE coppermind REFRESH COLLATION VERSION;' \
  && docker compose up -d
```

The reindex may take time and locks the affected indexes. The collation version
is refreshed only if the reindex succeeds; if it fails, fix the cause and rerun
the block. For an external database or additional databases, coordinate the
equivalent repair with the database administrator.

## Named tests this data model is proven against

These identifiers appear throughout the delivery plan and the acceptance
matrix in [delivery-plan.md](delivery-plan.md); they are listed here because
they are the concrete evidence that the shapes above behave as described:
`T-FR-1` (a device edit is picked up by the scan without any event),
`T-CE-1` (a stale `If-Match` is refused), `T-ID-1`/`T-ID-2` (identity
survives a rename and a move), `T-ID-3` (`rebuild_metadata` recovers the
same identifiers from disk alone), `T-SRC-1` (a source cannot be mutated
through any route).


## Deleted-note recovery receipts

`/data/state/note-recovery/<note-id>-<removal-commit>.json` is immutable,
versioned recovery evidence with `schema_version`, `note_id`, `path`,
`content_hash`, `removal_commit`, Git `blob`, base64 `content`, and
`recorded_at`. The content hash is verified before recovery. Receipts are
mode 0600 and contain private note contents, so they belong in the protected
backup set, never operational logs or exported diagnostics. Restore
confirmations live under its `restores/` subdirectory. Later observations of
returned identities live under `observed/`, so a rebuild cannot confuse an
old deletion with a later return. None of these files is synced.

Rebuild seeds identities from receipts before scanning files. Observed file
contents always win when an identity returns. `deleted` is reversible absence
with Git recovery evidence, not an instruction to erase files or sources.
Only a confirmed [retention purge](#retention-purge-receipts) removes a
receipt and its observation copies.
Historical source approvals remain in their existing review/filing receipts.

A complete backup must preserve `/data/notes` including `.git`, `/data/sources`,
all `/data/state` including recovery and approval receipts, and the separately
mounted credentials and sync state described in the acceptance plan. Copying
Markdown alone is insufficient. Git snapshots on the same volume are not an
off-device backup. Full backup tooling and its isolated restore drill remain
separate work.

Obsidian Sync preferences are edited under **Admin > Obsidian Sync**, What is
synced (`/admin/sync/what`). The helper
applies saved preferences at startup and checks them every 15 seconds. Save and
apply requests an immediate attempt. The live table reads the client configuration;
it does not infer successful application from saved values. Failed application stays
visible and retries automatically. A paused client stays paused during configuration.

The helper publishes its state in `/data/state/sync/status.json`, including
`needs_signin` (Obsidian signed this server out; sync waits for a sign-in in
Admin) and `account_known` (the helper remembers which Obsidian account it
connected with, so signing in again asks only for the password). That account
is kept in `/var/lib/obsidian-sync/coppermind-account` on the helper's
credential volume, never under `/data`, and is forgotten on disconnect. The
devices that changed notes most recently, with the space the vault uses, are
published in `/data/state/sync/devices.json`.

All attachment categories are enabled for new installations, including `unsupported`
for ICS and ZIP. Existing selections are preserved; tick the kinds of files under What
is synced to update an existing vault. The uploading device must also enable those file types.
Settings synchronization remains off by default. Obsidian's Show all files preference
is a device display setting, not a server sync control.

The plan and byte limits are local reference values, not client configuration or a
subscription change. The calendar intake limit remains independent of Sync limits.

Completed Dropbox receipts clear after a fixed seven days. Failed receipts never
expire automatically. Originals and import history
remain under `/data/state/calendars` after receipt expiry or dismissal.

## Retention purge receipts

**Admin > Storage** sets the keep time, `retention.days`, with revision safety,
and **Storage > Clean up** (`/admin/storage/cleanup`) lists what is past it and
deletes nothing until that list is confirmed. A list that changed after it was
shown is shown again rather than acted on. No timer erases data. The retention window begins
at the later of the deleted row's last update and its recovery receipt's
recorded time. The boundary is inclusive. Missing notes and notes still in
Trash are not deleted-note candidates.

An orphaned source is eligible independently of that window when its manifest
identifies an opening note that is deleted, or a completed purge receipt proves
that note was purged, and no live note cites the source. Missing and unparsed
mirror rows are protected as potential live references. The store also reads
current files, including unmirrored notes and Trash, before selecting sources.
Unreadable notes refuse cleanup; legacy bundles without an opening-note identity
are conservatively kept. A source's external-identity claim is removed with its
bundle so a later ingest can create a new source normally.

A removed artifact's archive under `/data/artifacts/<id>` is a candidate once its
manifest's `removed_at` is older than the same window (inclusive boundary) and its
`removed_reason` is one of the owner's own removals: `removed in Admin`, `removed through
the API` or `deleted in Obsidian`. An archive retired for any other cause, such as a
browsable copy absent at a rebuild, is never a candidate, so a device-side loss never
discards the original. Each candidate carries that `reason`. Only an archive marked removed
and held by no mirror row is listed; a live artifact is never a
candidate. Confirmation deletes the archive's files and directories after rechecking their
hashes, through the same dry run, confirm and abandon flow as notes and sources. A note that
still cites the artifact keeps its citation.

`/data/state/purges/<token>.json` holds a preview or unfinished receipt.
Completed receipts move to `purges/done/` and abandoned ones to
`purges/abandoned/`, so reconciliation reads only the receipts that can still
hold identities. A receipt file that cannot be read is skipped, reported in
Problems, and moved to `purges/quarantine/` by Abandon. Each receipt contains the preview, candidate metadata,
file hashes, authorization state, and completion outcome. It contains no recovery
body or artifact content. Confirmation rechecks settings, the candidate set,
mirror fingerprints and file hashes. Any change requires a new preview. Normal
store writers overlap, but purge holds an exclusive maintenance barrier against
note writes, ingest, reconciliation, recovery, filing and search-index publication. Filesystem sync writers
remain external to that barrier; the store checks their latest files immediately
before removal and never removes a live note file.

The durable `applying` receipt precedes any removal, and the database commit
precedes every unlink: mirror rows and search entries for the whole list go
together in one transaction, and only then are files unlinked (a source's
`manifest.json` first, so reconciliation and recovery ignore a half-removed
bundle). A failure before the commit removes nothing and leaves the preview
usable. A failure after it leaves a `failed` receipt whose rows are already gone
and whose files may remain. The failure appears in Problems and on Storage, Clean up; the next preview returns that saved list for explicit retry. Retry
verifies surviving files and live references, then completes only the originally
authorized work. Recovery skips pending purge identities so partial cleanup cannot
recreate their recovery copies. **Abandon** (Cancel on Admin's Storage, Clean up, or
`/v1/admin/purge/abandon`) is the way out of a stuck `applying` or `failed`
receipt: it retries nothing, records which listed items are gone and which
remain (`remaining`, shown in Problems), and lets a fresh preview start. Files an
abandoned receipt recorded and left behind are offered again by the next preview
as leftovers, but only while they are unchanged, nothing else sits in their
bundle, and no live note cites them. A source
row whose bundle holds no files, or a deleted-note row whose recovery receipt
is gone, is listed as a leftover from an interrupted purge and its row is
removed on confirm. A bundle with files but no readable manifest is damaged: it
is never offered, and Problems reports it. No automatic destructive retry
runs at startup. Successful confirmation is idempotent. Purge removes all saved
recovery generations and later-observation copies for each selected note,
source bundles and generated projections for selected sources, mirror rows,
and search entries through the indexer's removal functions. Receipts and Git's
removal commits remain as audit evidence. Git history is never rewritten.

Counts show notes and sources separately, and each origin and provider shows its
own deleted-note and orphaned-source counts. Notes inherit their associated
sources' origins/providers; notes with none use Local notes. A note with several
origins/providers counts once in each applicable group, so grouped totals can
exceed the number of candidates. The preview also reports what it kept:
`kept.notes_within_retention` and `kept.sources_in_use`. The Home Needs attention
list includes unfinished purges, abandoned purges with items remaining, unreadable
receipts and damaged bundles (`purge_problems`), so it agrees with the Problems list.
Status stays cheap: it reads `/data/state/purge-problems.json`, the few receipts that
can still be unfinished and one bounded query on the ids an abandoned purge left, and
never walks a source bundle or a finished receipt. The full scan that rewrites that file
runs only at store startup and after a completed preview, confirm or abandon; a failed
confirm leaves it as it was. Between scans, work that actually observes a bundle updates
the file: a preview that finds a bundle unreadable or readable again, and a metadata
rebuild whose source recovery fails to verify, or later verifies, a bundle. Each record
keeps what found it: an unreadable manifest is cleared when purge reads it again, and a
bundle recovery could not verify (a missing file, a mismatch) is cleared only when a
rebuild verifies it or a confirmed purge removes it. Purge planning never clears the latter. Reconciliation
does not read source bundles or finished receipts, so it records nothing. An item an
abandoned purge left drops out of the Problem as soon as its note is live again or a live
note cites its source; that never makes it eligible for removal.
