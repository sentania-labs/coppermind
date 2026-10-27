# Note workflow and shared Dropbox

Enable **Admin > Note workflow** to add optional attendees,
invitees, calendar reference and time-unknown properties. Existing
notes, type choices and schema roles are preserved. Conflicting property types stop setup.
The common Dropbox defaults to `Dropbox` in the vault. Legacy per-calendar
folders retain priority and are not processed a second time by the common router.

## Review once

New valid unreviewed notes receive one initial enrichment pass when enrichment
is enabled. The existing backlog is opt-in. Ordinary edits do not run the model
again. **Refresh suggestions** explicitly requests another pass. Related calendar
changes can generate a calendar-only pass, without repeating filename or general
property proposals. An unrelated calendar change does not regenerate suggestions.

The **Review notes** screen shows the note and proposals together. Select changes
and complete review once, or leave everything unchecked to retain only your edits.
A filename change requires acknowledgement that existing filename links can need
repair. The authored heading and body stay unchanged. Stale suggestions cannot
be applied; reload or explicitly request fresh suggestions.

Install the companion ZIP from **Note workflow** into
`.obsidian/plugins/coppermind-review/`. Enable it in Community plugins. It requires
Obsidian 1.11 or later and a Coppermind API key with `notes:read`, `notes:write` and
`sources:read`. Select the key through Obsidian SecretStorage; the plugin saves only
its name, never its value, in plugin settings. Configure it separately on devices
that do not share local secret storage.

Open a note and run **Coppermind Review: Review current note**. The panel checks
that the exact local bytes match the server before permitting completion. Wait
for Obsidian Sync after editing. `date` is the one field for a note's date and
start time: `YYYY-MM-DD`, or `YYYY-MM-DDTHH:MM:SS` as a local date-and-time with no
UTC offset, matching Obsidian's Date and Date & time properties. Times are in the
configured timezone (`America/Chicago` by default), a setting rather than a note
property. New notes, ingest, enrichment and the review form write the start time
into `date`, and never write `time` or `timezone`. A `date` carrying a UTC offset
is rejected at review with a Problems message. Work notes (`type: work`) require a
start time in `date`, or `time_unknown: true` with a date-only `date`. Other note
types need only a day. Midnight is an actual time, never a sentinel for unknown.
Legacy notes with a `date` and an `HH:MM` `time` keep validating and filing, a
legacy `timezone` is ignored, and when a legacy `time` carries its own date and
disagrees with `date`, `date` wins and Problems reports a `date_mismatch` entry
naming both dates. Saving `date` from the review form or the Obsidian plugin, or
approving an enrichment date, removes the legacy `time` and `timezone`. An
enrichment `account` suggestion is added to the note's list and never replaces it.

`account` is a list of customer names. A plain string is read as a one-item list.
`account` is required when `context: external` and needs at least one entry; an
empty list fails review the same way a missing account does. A reference note
that applies to several customers lists them all, and a note found by any one of
them.

Review completion is recorded durably before writes and can be resumed. Each
selected change uses the existing version checks and approval receipts. The
reviewed flag is written last. Unselected proposals are dismissed. If someone
marks reviewed directly in Obsidian, finalization waits for an unchanged file
observation for at least 30 seconds and validates meeting metadata before filing.

Confirmed people records derive only from final reviewed attendee email addresses,
with note identity, version, date and properties as evidence. Names alone do not
create or merge identities. Edits replace a note's contribution; unreviewing or
losing access to a note withdraws it from the derived people view. These records
are a limited, evidence-backed dossier foundation, not automatic biographical or
company-profile synthesis.

## Calendar intake

Drop ICS files or ZIPs containing ICS files into Dropbox, or send binary bytes to
`POST /v1/dropbox?filename=export.ics` with `sources:write`. A 202 means the original
and intake receipt are durable, not that parsing has completed. Inspect Dropbox
in Admin for processing, identification and failure status. The vault scanner
waits 30 seconds for stable bytes. It never deletes dropped files.

Calendar-level VCALENDAR UID is the automatic identity. Event UID, filename,
calendar name and attendee overlap are not physical-calendar identity. Exports
without a calendar UID require a human choice of a separate new calendar or an
existing calendar. That choice applies to the import, not to future filenames.
If older configured calendars exist, an unfamiliar calendar UID also requires
one explicit choice. Binding it to the existing calendar preserves its event
history; subsequent exports with that UID route automatically.
ICS entries in a ZIP are identified separately, and all must be identified before
dispatch. Invalid or mixed archives do not partially dispatch during validation.
Per-calendar merge failures remain visible in individual import receipts; a
multi-calendar ZIP is not one atomic catalog transaction.

Originals and receipts remain under the state volume. Unsupported binary formats
are preserved with a visible unsupported status. Markdown in the vault Dropbox
continues through ordinary note adoption. The binary API does not adopt Markdown;
use the notes API for authored notes. Routing is isolated in `dropbox.py` so other
source handlers can be added without changing note reconciliation.

Existing calendar parser limits still apply: 10 MiB input, 40 MiB expanded ZIP,
100 archive entries, safe paths only, isolated parsing with CPU/memory limits.
Oversized vault files stay in place with a watcher notice. Partial exports retain
missing events; explicit cancellation is different from absence.

## Late calendar changes

Relevant event evidence is fingerprinted per note's date window. For reviewed
notes, changed evidence creates a follow-up with meeting candidates. Confirm an
association or dismiss the update in Admin or the companion. These decisions live
in sidecar state and never rewrite a reviewed note or infer attendance from an
invitation. Unchanged re-exports do not reopen the decision.

## State, recovery and rollback

Lifecycle receipts, confirmed contributions, follow-ups and Dropbox originals
are in the state volume and must be included in backups. PostgreSQL remains a
mirror. A complete review and completed monthly filing are separate recovery
steps. A filing failure keeps the note out of published search until corrected.

This version adds context configuration fields. Before upgrade, back up the
state volume. Older binaries do not understand those fields; a rollback needs
the compatible pre-upgrade context configuration as well as the earlier images.
Do not restore old note files over newer reviewed content.

## API for companions

- `GET /v1/notes/{id}/review`: note, current proposals and follow-up status.
  Requires `notes:read` and `sources:read` because evidence can reference sources.
- `POST /v1/notes/{id}/review`: explicit completion with `if_match`, selected
  proposal indexes and optional filename-risk acknowledgement. Also requires
  `notes:write`. Repeating the recorded request is safe.
- `POST /v1/notes/{id}/suggestions`: explicit refresh, with the same three scopes.
- `POST /v1/notes/{id}/calendar-update`: confirm a candidate or dismiss the update,
  conditional on both note version and calendar evidence version.

The companion uses the documented Obsidian
[SecretStorage](https://docs.obsidian.md/plugins/guides/secret-storage) and
[requestUrl](https://docs.obsidian.md/Reference/TypeScript%20API/requestUrl) APIs.
