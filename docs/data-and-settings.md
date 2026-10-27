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
| Optional keys | `date` (`YYYY-MM-DD`, or a local date-and-time `YYYY-MM-DDTHH:MM:SS` with no UTC offset in the configured timezone, `general.timezone`, `America/Chicago` by default; the one field for a note's date and start time, written as a date-and-time on new notes; a UTC offset is rejected at review validation), `time_unknown` (bool; a work note with a date-only `date` needs it), `account` (a list of customer names; a plain string is read as a one-item list; at least one entry is required when `context: external`), `tags` (list), `title` (only when it differs from the heading), `external_refs` (a passthrough map), `unresolved` (set by the curator on an ambiguous filing), `managed` (projections only). A legacy `HH:MM` `time` beside a `date` still validates and files, and a legacy `timezone` is ignored; when a legacy `time` carries its own date and disagrees with `date`, `date` wins and Problems records a `date_mismatch` entry |
| Shipped `type` vocabulary | `work`, `personal`, `reference`, `journal` (default: `reference`) |
| Shipped `context` vocabulary | `internal`, `external`, `personal` |
| Filenames | Dated types: `YYYY-MM-DD Title.md`. Undated: `Title.md`. Journal: `Journal/YYYY/YYYY-MM-DD.md`. Names are Unicode-normalized, Windows-reserved characters and device names are stripped, capped at 120 characters, and a case-insensitive collision gets a numbered suffix |
| Identity (`ETag`) | `"sha256:<hex of the file's bytes>"`. A move does not change it; a frontmatter write-back does |
| Unparseable file | Left byte-for-byte untouched, recorded as `state: unparsed` with a reason, indexed by body if it can be decoded at all |
| Duplicate ID | The path the mirror last recorded keeps the ID; the other file gets a fresh one written back after it settles |
| ID assignment for a device-created file | The reconciler writes the ID, and any other frontmatter the schema requires, once the file has gone 30 seconds (configurable) without an mtime change, using a compare-and-swap on the content hash so a still-in-progress edit is never interrupted |

**Field guidance and tags** live in the same `schema.yaml`. Each key may
carry `vocabulary_meanings` (a one-line meaning per allowed value) and a
`guidance` paragraph for whoever fills it in; neither changes validation.
Three top-level entries make tags data: `tags` (each listed tag and its
meaning), `tag_aliases` (an alias and the canonical tag it is replaced by on
every write), and `tag_mode`, `open` by default (any tag accepted and counted)
or `closed` (a write that adds an unlisted tag is refused). Admin's Fields and
tags page edits all of it under the file's revision and shows every tag in use
with its note count from the mirror. Merging or renaming a tag across existing
notes is a later job, not part of this page.

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

Every key below has a working default and a place in Admin to change it.
Nothing here needs to be set before the system runs.

| Section | Key | Default |
|---|---|---|
| general | `timezone` | `America/Chicago` |
| notes | `review_folder` | `Review` |
| notes | `trash_folder` | `_Trash` |
| notes | `sources_folder` | `_Sources` |
| notes | `attachments_folder` | `_Attachments` |
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
| events | `retention_days` | 7 |
| events | `poll_fallback_s` | 5 |
| limits | `ingest_max_bytes` | 25 MiB |
| limits | `attachment_max_bytes` | `sync.max_file_bytes` |
| admin | `session_hours` | 12 |

Every control state file, including this one, carries `schema_version` and
`revision`. A write must state the revision it is replacing, and the file it
replaces moves to `/data/state/history/`, so nothing is ever silently lost
to a concurrent edit.

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
`admin.json`: the argon2 hash of the claimed password and when it was
claimed. Both are files under `/data/state`, mode 0600, never database rows,
because the API-key cache and the admin session both need to keep working
even if PostgreSQL is unavailable.

## The PostgreSQL mirror

Nothing here is durable on its own; the `rebuild_metadata` job recreates all
of it from `/data`. Tables exist for: note metadata (path, hashes,
frontmatter fields worth querying by, observed state), source and revision
and artifact records, which note cites which source, the event outbox each
consumer reads from and acknowledges, each consumer's own read cursor, the
generated search index, a mirror of API key hashes, per-key last-used
timestamps, a mirror of every control-state file's current revision, job
history, per-component status (as reported by the helpers' own status
files), and admin sessions. None of it is the durable copy of anything; all
of it is a cache that makes listing and search fast.

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
Historical source approvals remain in their existing review/filing receipts.

A complete backup must preserve `/data/notes` including `.git`, `/data/sources`,
all `/data/state` including recovery and approval receipts, and the separately
mounted credentials and sync state described in the acceptance plan. Copying
Markdown alone is insufficient. Git snapshots on the same volume are not an
off-device backup. Full backup tooling and its isolated restore drill remain
separate work.

Obsidian Sync preferences are edited under **Admin > Obsidian Sync**. The helper
applies saved preferences at startup and checks them every 15 seconds. Save and
apply requests an immediate attempt. The live table reads the client configuration;
it does not infer successful application from saved values. Failed application stays
visible and retries automatically. A paused client stays paused during configuration.

All attachment categories are enabled for new installations, including `unsupported`
for ICS and ZIP. Existing selections are preserved; use **Sync all file types** to
update an existing vault. The uploading device must also enable those file types.
Settings synchronization remains off by default. Obsidian's Show all files preference
is a device display setting, not a server sync control.

The plan and byte limits are local reference values, not client configuration or a
subscription change. The calendar intake limit remains independent of Sync limits.
